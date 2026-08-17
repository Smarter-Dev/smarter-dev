#!/usr/bin/env python
"""Find the busiest recent day in a Discord channel and pull it as a fixture.

Two subcommands:

  scan  — page backwards through recent #general history, bucket messages by
          UTC day, print a per-day engagement table and recommend the busiest
          day (engagement score = human messages x distinct human authors).
  pull  — fetch every message in one UTC day and write it to
          scripts/proactive_eval/data/ as a JSONL fixture plus a meta sidecar.
          The data dir is gitignored: fixtures hold real member messages and
          must never be committed.

Usage:
    uv run python -m scripts.proactive_eval.fetch_history scan [--days 45]
    uv run python -m scripts.proactive_eval.fetch_history pull --date 2026-08-08

Both read the bot token from PROD_DISCORD_BOT_TOKEN (via .env) unless
--token-env names another variable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from smarter_dev.web.discord_rest import DiscordBotClient, DiscordRestError  # noqa: E402

DISCORD_EPOCH_MS = 1420070400000
PAGE_LIMIT = 100
PAGE_DELAY_SECONDS = 0.25
DEFAULT_RETRY_AFTER_SECONDS = 1.0
MAX_RATE_LIMIT_RETRIES = 5
REPLY_MESSAGE_TYPE = 19
TEXT_CHANNEL_TYPE = 0
DATA_DIR = Path(__file__).resolve().parent / "data"


def snowflake_from_unix_ms(unix_ms: int) -> int:
    return (unix_ms - DISCORD_EPOCH_MS) << 22


def unix_ms_from_snowflake(snowflake: int) -> int:
    return (snowflake >> 22) + DISCORD_EPOCH_MS


def day_bounds_ms(day: date) -> tuple[int, int]:
    """Unix-ms bounds of ``day`` as the half-open UTC interval [start, end)."""
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    start_ms = int(day_start.timestamp()) * 1000
    end_ms = int((day_start + timedelta(days=1)).timestamp()) * 1000
    return start_ms, end_ms


def normalized_channel_name(name: str) -> str:
    """Drop leading emoji decoration (anything before the first ASCII
    letter, digit, ``-`` or ``_``) so ``--channel general`` matches the
    guild's ``💬general``."""
    for index, character in enumerate(name):
        if character.isascii() and (character.isalnum() or character in "-_"):
            return name[index:]
    return name


def retry_after_seconds(error: DiscordRestError) -> float:
    """Pull ``retry_after`` out of a 429 error's embedded response body."""
    message = str(error)
    body_start = message.find("{")
    if body_start == -1:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        body = json.loads(message[body_start:])
    except json.JSONDecodeError:
        return DEFAULT_RETRY_AFTER_SECONDS
    retry_after = body.get("retry_after")
    if not isinstance(retry_after, int | float):
        return DEFAULT_RETRY_AFTER_SECONDS
    return float(retry_after)


def message_record(raw: dict) -> dict:
    """Convert a raw Discord message into the eval's JSONL schema.

    Stages 2-4 all consume this schema; keep it exact.
    """
    author = raw["author"]
    member = raw.get("member") or {}
    reply_to_id = None
    if raw.get("type") == REPLY_MESSAGE_TYPE:
        reply_to_id = (raw.get("message_reference") or {}).get("message_id")
    reaction_counts = {}
    for reaction in raw.get("reactions") or []:
        emoji = reaction.get("emoji") or {}
        emoji_key = emoji.get("name") or str(emoji.get("id"))
        reaction_counts[emoji_key] = reaction.get("count", 0)
    return {
        "id": raw["id"],
        "timestamp": datetime.fromisoformat(raw["timestamp"])
        .astimezone(UTC)
        .isoformat(),
        "author_id": author["id"],
        "author_name": author["username"],
        "author_display": member.get("nick")
        or author.get("global_name")
        or author["username"],
        "is_bot": author.get("bot", False),
        "content": raw.get("content", ""),
        "reply_to_id": reply_to_id,
        "mention_user_ids": [m["id"] for m in raw.get("mentions") or []],
        "mention_everyone": raw.get("mention_everyone", False),
        "attachment_count": len(raw.get("attachments") or []),
        "sticker_count": len(raw.get("sticker_items") or []),
        "reaction_counts": reaction_counts,
        "message_type": raw.get("type", 0),
    }


@dataclass
class DayStats:
    day: date
    total_messages: int
    human_messages: int
    distinct_human_authors: int

    @property
    def engagement_score(self) -> int:
        return self.human_messages * self.distinct_human_authors


def day_stats(raw_messages: list[dict]) -> list[DayStats]:
    """Bucket raw messages by UTC day, newest day first."""
    totals: dict[date, int] = {}
    human_counts: dict[date, int] = {}
    human_authors: dict[date, set[str]] = {}
    for raw in raw_messages:
        day = datetime.fromisoformat(raw["timestamp"]).astimezone(UTC).date()
        totals[day] = totals.get(day, 0) + 1
        if not raw["author"].get("bot", False):
            human_counts[day] = human_counts.get(day, 0) + 1
            human_authors.setdefault(day, set()).add(raw["author"]["id"])
    return [
        DayStats(
            day=day,
            total_messages=totals[day],
            human_messages=human_counts.get(day, 0),
            distinct_human_authors=len(human_authors.get(day, set())),
        )
        for day in sorted(totals, reverse=True)
    ]


def recommend_day(stats: list[DayStats]) -> DayStats:
    if not stats:
        raise ValueError("no days to recommend: the scan returned no messages")
    return max(stats, key=lambda s: (s.engagement_score, s.day))


@dataclass(kw_only=True)
class HistoryClient(DiscordBotClient):
    """Read-only history fetcher over the shared bot-token REST client.

    ``page_limit`` is Discord's page size; a page shorter than it means the
    channel has no more messages in that direction. Tests shrink it to match
    their mock transports.
    """

    page_limit: int = PAGE_LIMIT

    user_agent = "SmarterDevProactiveEval/1.0"

    async def _get_json(self, endpoint: str, params: dict | None = None):
        """GET with retries on Discord 429s; every other error propagates."""
        attempts = 0
        while True:
            try:
                response = await self._request("GET", endpoint, params=params or {})
            except self.error_type as error:
                if error.status_code != 429:
                    raise
                attempts += 1
                if attempts > MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = retry_after_seconds(error)
                _progress(f"rate limited; retrying in {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            return response.json()

    async def current_user(self) -> dict:
        return await self._get_json("/users/@me")

    async def guilds(self) -> list[dict]:
        return await self._get_json("/users/@me/guilds")

    async def guild_channels(self, guild_id: str) -> list[dict]:
        return await self._get_json(f"/guilds/{guild_id}/channels")

    async def message_page(
        self,
        channel_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict]:
        params: dict = {"limit": self.page_limit}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return await self._get_json(f"/channels/{channel_id}/messages", params)

    async def scan_messages(
        self,
        channel_id: str,
        *,
        oldest_ms: int,
        page_delay: float = PAGE_DELAY_SECONDS,
    ) -> list[dict]:
        """Page backwards from now until messages get older than ``oldest_ms``."""
        kept: list[dict] = []
        before: str | None = None
        page_number = 0
        while True:
            page = await self.message_page(channel_id, before=before)
            if not page:
                break
            page_number += 1
            page = sorted(page, key=lambda m: int(m["id"]), reverse=True)
            before = page[-1]["id"]
            in_range = [
                m
                for m in page
                if unix_ms_from_snowflake(int(m["id"])) >= oldest_ms
            ]
            kept.extend(in_range)
            oldest_seen = datetime.fromtimestamp(
                unix_ms_from_snowflake(int(page[-1]["id"])) / 1000, tz=UTC
            )
            _progress(
                f"scan: page {page_number}, {len(kept)} messages kept, "
                f"oldest seen {oldest_seen:%Y-%m-%d %H:%M} UTC"
            )
            if len(in_range) < len(page) or len(page) < self.page_limit:
                break
            if page_delay:
                await asyncio.sleep(page_delay)
        return kept

    async def pull_day_messages(
        self,
        channel_id: str,
        *,
        day_start_ms: int,
        day_end_ms: int,
        page_delay: float = PAGE_DELAY_SECONDS,
    ) -> list[dict]:
        """Fetch every message in [day_start_ms, day_end_ms), ascending."""
        collected: list[dict] = []
        # The largest snowflake before the day starts, so the first page
        # begins exactly at the day boundary (``after`` is exclusive).
        after = str(snowflake_from_unix_ms(day_start_ms) - 1)
        page_number = 0
        while True:
            page = await self.message_page(channel_id, after=after)
            if not page:
                break
            page_number += 1
            page = sorted(page, key=lambda m: int(m["id"]))
            after = page[-1]["id"]
            in_day = [
                m
                for m in page
                if day_start_ms
                <= unix_ms_from_snowflake(int(m["id"]))
                < day_end_ms
            ]
            collected.extend(in_day)
            newest_seen = datetime.fromtimestamp(
                unix_ms_from_snowflake(int(page[-1]["id"])) / 1000, tz=UTC
            )
            _progress(
                f"pull: page {page_number}, {len(collected)} messages collected, "
                f"reached {newest_seen:%Y-%m-%d %H:%M} UTC"
            )
            if len(in_day) < len(page) or len(page) < self.page_limit:
                break
            if page_delay:
                await asyncio.sleep(page_delay)
        return collected


async def resolve_guild(
    client: HistoryClient, *, guild_id: str | None, guild_name: str
) -> dict:
    guilds = await client.guilds()
    for guild in guilds:
        if guild_id is not None:
            if guild["id"] == guild_id:
                return guild
        elif guild["name"].casefold() == guild_name.casefold():
            return guild
    wanted = guild_id if guild_id is not None else guild_name
    available = ", ".join(sorted(g["name"] for g in guilds)) or "(none)"
    raise SystemExit(
        f"Guild {wanted!r} not found. Bot is in: {available}"
    )


async def resolve_channel(
    client: HistoryClient,
    guild_id: str,
    *,
    channel_id: str | None,
    channel_name: str,
) -> dict:
    channels = await client.guild_channels(guild_id)
    for channel in channels:
        if channel_id is not None:
            if channel["id"] == channel_id:
                return channel
        elif (
            channel.get("type") == TEXT_CHANNEL_TYPE
            and normalized_channel_name(channel["name"]).casefold()
            == normalized_channel_name(channel_name).casefold()
        ):
            return channel
    wanted = channel_id if channel_id is not None else channel_name
    available = ", ".join(
        sorted(
            c["name"] for c in channels if c.get("type") == TEXT_CHANNEL_TYPE
        )
    ) or "(none)"
    raise SystemExit(
        f"Channel {wanted!r} not found. Text channels: {available}"
    )


def build_meta(
    *,
    guild_id: str,
    guild_name: str,
    channel_id: str,
    channel_name: str,
    day: date,
    bot_user_id: str,
    fetched_at: str,
    records: list[dict],
) -> dict:
    human_records = [r for r in records if not r["is_bot"]]
    return {
        "guild_id": guild_id,
        "guild_name": guild_name,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "date": day.isoformat(),
        "fetched_at": fetched_at,
        "message_count": len(records),
        "human_message_count": len(human_records),
        "distinct_human_authors": len({r["author_id"] for r in human_records}),
        "bot_user_id": bot_user_id,
    }


def write_fixture(
    records: list[dict], meta: dict, *, data_dir: Path
) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{meta['guild_id']}-{meta['channel_name']}-{meta['date']}"
    jsonl_path = data_dir / f"{stem}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as jsonl_file:
        for record in records:
            jsonl_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    meta_path = data_dir / f"{stem}.meta.json"
    meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return jsonl_path, meta_path


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _make_client(token_env: str) -> HistoryClient:
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(
            f"Missing {token_env} — set it in the environment or {REPO_ROOT / '.env'}"
        )
    return HistoryClient(bot_token=token)


async def _resolve_target(client: HistoryClient, args: argparse.Namespace) -> tuple[dict, dict]:
    guild = await resolve_guild(
        client, guild_id=args.guild_id, guild_name=args.guild_name
    )
    channel = await resolve_channel(
        client, guild["id"], channel_id=args.channel_id, channel_name=args.channel
    )
    return guild, channel


async def run_scan(args: argparse.Namespace) -> None:
    client = _make_client(args.token_env)
    guild, channel = await _resolve_target(client, args)
    _progress(
        f"scanning #{channel['name']} in {guild['name']} "
        f"over the last {args.days} days"
    )
    oldest_ms = round(
        (datetime.now(UTC) - timedelta(days=args.days)).timestamp() * 1000
    )
    messages = await client.scan_messages(channel["id"], oldest_ms=oldest_ms)
    stats = day_stats(messages)
    if not stats:
        raise SystemExit("No messages found in the lookback window.")

    print(f"{'date':<12}{'total':>7}{'human':>7}{'authors':>9}{'score':>8}")
    for day in stats:
        print(
            f"{day.day.isoformat():<12}{day.total_messages:>7}"
            f"{day.human_messages:>7}{day.distinct_human_authors:>9}"
            f"{day.engagement_score:>8}"
        )
    best = recommend_day(stats)
    print(
        f"\nBusiest day: {best.day.isoformat()} — {best.human_messages} human "
        f"messages from {best.distinct_human_authors} authors "
        f"(score {best.engagement_score}). Pull it with:"
    )
    print(
        f"  uv run python -m scripts.proactive_eval.fetch_history pull "
        f"--date {best.day.isoformat()} "
        f"--guild-id {guild['id']} --channel-id {channel['id']}"
    )


async def run_pull(args: argparse.Namespace) -> None:
    client = _make_client(args.token_env)
    guild, channel = await _resolve_target(client, args)
    _progress(
        f"pulling {args.date.isoformat()} from #{channel['name']} "
        f"in {guild['name']}"
    )
    day_start_ms, day_end_ms = day_bounds_ms(args.date)
    raw_messages = await client.pull_day_messages(
        channel["id"], day_start_ms=day_start_ms, day_end_ms=day_end_ms
    )
    records = [message_record(raw) for raw in raw_messages]
    bot_user = await client.current_user()
    meta = build_meta(
        guild_id=guild["id"],
        guild_name=guild["name"],
        channel_id=channel["id"],
        channel_name=channel["name"],
        day=args.date,
        bot_user_id=bot_user["id"],
        fetched_at=datetime.now(UTC).isoformat(),
        records=records,
    )
    jsonl_path, meta_path = write_fixture(records, meta, data_dir=DATA_DIR)
    print(
        f"Wrote {meta['message_count']} messages "
        f"({meta['human_message_count']} human, "
        f"{meta['distinct_human_authors']} authors) to:\n"
        f"  {jsonl_path}\n  {meta_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_history",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_shared_flags(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--channel", default="general")
        subparser.add_argument("--guild-name", default="Smarter Dev")
        subparser.add_argument("--guild-id", default=None)
        subparser.add_argument("--channel-id", default=None)
        subparser.add_argument("--token-env", default="PROD_DISCORD_BOT_TOKEN")

    scan = subparsers.add_parser(
        "scan", help="rank recent days by engagement and recommend the busiest"
    )
    add_shared_flags(scan)
    scan.add_argument("--days", type=int, default=45)

    pull = subparsers.add_parser(
        "pull", help="fetch one UTC day into a JSONL fixture"
    )
    add_shared_flags(pull)
    pull.add_argument("--date", type=date.fromisoformat, required=True)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "scan":
        asyncio.run(run_scan(args))
    else:
        asyncio.run(run_pull(args))


if __name__ == "__main__":
    main()
