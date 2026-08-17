"""Tests for scripts/proactive_eval/fetch_history.py.

All Discord traffic goes through httpx.MockTransport handlers serving canned
JSON; nothing here touches the network.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval import fetch_history  # noqa: E402

from smarter_dev.web.discord_rest import DiscordRestError  # noqa: E402


def _unix_ms(iso_timestamp: str) -> int:
    return round(datetime.fromisoformat(iso_timestamp).timestamp() * 1000)


def _raw_message(
    unix_ms: int,
    *,
    author_id: str = "1",
    username: str = "alice",
    bot: bool = False,
    **overrides,
) -> dict:
    """A minimal raw Discord message whose id encodes ``unix_ms``."""
    message = {
        "id": str(fetch_history.snowflake_from_unix_ms(unix_ms)),
        "type": 0,
        "timestamp": datetime.fromtimestamp(unix_ms / 1000, tz=UTC).isoformat(),
        "content": "hello",
        "author": {
            "id": author_id,
            "username": username,
            "global_name": None,
            "bot": bot,
        },
        "mentions": [],
        "mention_everyone": False,
        "attachments": [],
    }
    message.update(overrides)
    return message


def _history_transport(
    messages: list[dict], requests: list[httpx.Request], page_size: int
) -> httpx.MockTransport:
    """Serve /channels/.../messages pages the way Discord does.

    Pages are returned newest-first regardless of cursor direction, so the
    fetcher's local sorting is what the tests actually exercise.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = dict(request.url.params)
        pool = sorted(messages, key=lambda m: int(m["id"]), reverse=True)
        if "before" in params:
            pool = [m for m in pool if int(m["id"]) < int(params["before"])]
        if "after" in params:
            pool = [m for m in pool if int(m["id"]) > int(params["after"])]
            # Discord serves the page closest to the ``after`` cursor.
            pool = sorted(pool, key=lambda m: int(m["id"]))[:page_size]
            return httpx.Response(
                200, json=sorted(pool, key=lambda m: int(m["id"]), reverse=True)
            )
        return httpx.Response(200, json=pool[:page_size])

    return httpx.MockTransport(handle)


# --- snowflake math ---------------------------------------------------------


def test_snowflake_round_trip():
    for unix_ms in (1420070400000, 1462015105796, 1754870400123):
        snowflake = fetch_history.snowflake_from_unix_ms(unix_ms)
        assert fetch_history.unix_ms_from_snowflake(snowflake) == unix_ms


def test_snowflake_known_vector():
    # Discord docs example: snowflake 175928847299117063 was created at
    # 2016-04-30T11:18:25.796Z (unix ms 1462015105796).
    assert fetch_history.unix_ms_from_snowflake(175928847299117063) == 1462015105796


def test_snowflake_epoch_is_zero():
    assert fetch_history.snowflake_from_unix_ms(1420070400000) == 0


def test_day_bounds_are_utc_midnights():
    start_ms, end_ms = fetch_history.day_bounds_ms(date(2026, 8, 8))
    assert start_ms == _unix_ms("2026-08-08T00:00:00+00:00")
    assert end_ms == _unix_ms("2026-08-09T00:00:00+00:00")


# --- backward pagination (scan) --------------------------------------------


async def test_scan_pages_backwards_and_stops_at_lookback_bound():
    timestamps = [
        "2026-08-10T10:00:00+00:00",
        "2026-08-10T09:00:00+00:00",
        "2026-08-09T15:00:00+00:00",
        "2026-08-09T14:00:00+00:00",
        "2026-08-01T10:00:00+00:00",  # older than the bound
        "2026-07-20T10:00:00+00:00",  # older still; must never be needed
    ]
    messages = [_raw_message(_unix_ms(ts)) for ts in timestamps]
    requests: list[httpx.Request] = []
    client = fetch_history.HistoryClient(
        bot_token="tok",
        transport=_history_transport(messages, requests, page_size=2),
        page_limit=2,
    )

    kept = await client.scan_messages(
        "C", oldest_ms=_unix_ms("2026-08-05T00:00:00+00:00"), page_delay=0
    )

    kept_ids = [m["id"] for m in kept]
    assert kept_ids == [m["id"] for m in messages[:4]]

    before_cursors = [
        int(dict(r.url.params)["before"])
        for r in requests
        if "before" in dict(r.url.params)
    ]
    assert before_cursors == sorted(before_cursors, reverse=True)
    # Stops on the page that crossed the bound: 3 pages of 2, not 4.
    assert len(requests) == 3


async def test_scan_stops_on_short_page_at_channel_start():
    messages = [
        _raw_message(_unix_ms("2026-08-10T10:00:00+00:00")),
        _raw_message(_unix_ms("2026-08-10T09:00:00+00:00")),
        _raw_message(_unix_ms("2026-08-10T08:00:00+00:00")),
    ]
    requests: list[httpx.Request] = []
    client = fetch_history.HistoryClient(
        bot_token="tok",
        transport=_history_transport(messages, requests, page_size=100),
    )
    kept = await client.scan_messages(
        "C", oldest_ms=_unix_ms("2026-08-01T00:00:00+00:00"), page_delay=0
    )
    assert len(kept) == 3
    assert len(requests) == 1


# --- forward pagination (pull) ----------------------------------------------


async def test_pull_collects_exactly_the_utc_day_ascending():
    inside = [
        "2026-08-08T00:00:00+00:00",
        "2026-08-08T09:30:00+00:00",
        "2026-08-08T15:00:00+00:00",
        "2026-08-08T23:59:59+00:00",
    ]
    outside = ["2026-08-07T23:59:59+00:00", "2026-08-09T00:00:00+00:00"]
    messages = [_raw_message(_unix_ms(ts)) for ts in inside + outside]
    requests: list[httpx.Request] = []
    client = fetch_history.HistoryClient(
        bot_token="tok",
        transport=_history_transport(messages, requests, page_size=2),
        page_limit=2,
    )

    day_start_ms, day_end_ms = fetch_history.day_bounds_ms(date(2026, 8, 8))
    pulled = await client.pull_day_messages(
        "C", day_start_ms=day_start_ms, day_end_ms=day_end_ms, page_delay=0
    )

    pulled_ms = [
        fetch_history.unix_ms_from_snowflake(int(m["id"])) for m in pulled
    ]
    assert pulled_ms == [_unix_ms(ts) for ts in inside]

    after_cursors = [
        int(dict(r.url.params)["after"])
        for r in requests
        if "after" in dict(r.url.params)
    ]
    assert after_cursors == sorted(after_cursors)


# --- day bucketing and engagement scoring -----------------------------------


def _stats_fixture() -> list:
    messages = [
        _raw_message(_unix_ms("2026-08-08T10:00:00+00:00"), author_id="1"),
        _raw_message(_unix_ms("2026-08-08T11:00:00+00:00"), author_id="2"),
        _raw_message(_unix_ms("2026-08-08T12:00:00+00:00"), author_id="1"),
        _raw_message(
            _unix_ms("2026-08-08T13:00:00+00:00"), author_id="9", bot=True
        ),
        _raw_message(_unix_ms("2026-08-09T10:00:00+00:00"), author_id="3"),
    ]
    return fetch_history.day_stats(messages)


def test_day_stats_buckets_by_utc_day_newest_first():
    stats = _stats_fixture()
    assert [s.day for s in stats] == [date(2026, 8, 9), date(2026, 8, 8)]


def test_day_stats_excludes_bots_from_human_counts():
    stats = _stats_fixture()
    busy = next(s for s in stats if s.day == date(2026, 8, 8))
    assert busy.total_messages == 4
    assert busy.human_messages == 3
    assert busy.distinct_human_authors == 2
    assert busy.engagement_score == 6


def test_recommend_day_picks_highest_engagement_score():
    stats = _stats_fixture()
    assert fetch_history.recommend_day(stats).day == date(2026, 8, 8)


def test_recommend_day_fails_on_empty_stats():
    with pytest.raises(ValueError):
        fetch_history.recommend_day([])


# --- JSONL record serialization ---------------------------------------------


def test_message_record_extracts_reply_only_for_reply_type():
    reply = _raw_message(
        _unix_ms("2026-08-08T10:00:00+00:00"),
        type=19,
        message_reference={"message_id": "777"},
    )
    assert fetch_history.message_record(reply)["reply_to_id"] == "777"

    system_pin = _raw_message(
        _unix_ms("2026-08-08T10:00:00+00:00"),
        type=6,
        message_reference={"message_id": "777"},
    )
    assert fetch_history.message_record(system_pin)["reply_to_id"] is None


def test_message_record_display_name_fallback_chain():
    base_ms = _unix_ms("2026-08-08T10:00:00+00:00")

    with_nick = _raw_message(base_ms, member={"nick": "zech"})
    with_nick["author"]["global_name"] = "Zech Z"
    assert fetch_history.message_record(with_nick)["author_display"] == "zech"

    with_global = _raw_message(base_ms)
    with_global["author"]["global_name"] = "Zech Z"
    assert (
        fetch_history.message_record(with_global)["author_display"] == "Zech Z"
    )

    username_only = _raw_message(base_ms, username="zzmmrmn")
    assert (
        fetch_history.message_record(username_only)["author_display"]
        == "zzmmrmn"
    )


def test_message_record_counts_and_reactions():
    raw = _raw_message(
        _unix_ms("2026-08-08T10:00:00+00:00"),
        mentions=[{"id": "5"}, {"id": "6"}],
        mention_everyone=True,
        attachments=[{"id": "a"}],
        sticker_items=[{"id": "s1"}, {"id": "s2"}],
        reactions=[
            {"emoji": {"id": None, "name": "👍"}, "count": 2},
            {"emoji": {"id": "123", "name": "blobwave"}, "count": 1},
        ],
    )
    record = fetch_history.message_record(raw)
    assert record["mention_user_ids"] == ["5", "6"]
    assert record["mention_everyone"] is True
    assert record["attachment_count"] == 1
    assert record["sticker_count"] == 2
    assert record["reaction_counts"] == {"👍": 2, "blobwave": 1}
    assert record["message_type"] == 0
    assert record["is_bot"] is False
    assert record["content"] == "hello"


def test_message_record_timestamp_is_utc_iso():
    raw = _raw_message(_unix_ms("2026-08-08T14:03:22.123000+00:00"))
    record = fetch_history.message_record(raw)
    assert record["timestamp"] == "2026-08-08T14:03:22.123000+00:00"


# --- fixture files -----------------------------------------------------------


def test_write_fixture_writes_jsonl_and_meta(tmp_path):
    records = [
        fetch_history.message_record(
            _raw_message(_unix_ms("2026-08-08T10:00:00+00:00"), author_id="1")
        ),
        fetch_history.message_record(
            _raw_message(_unix_ms("2026-08-08T11:00:00+00:00"), author_id="2")
        ),
        fetch_history.message_record(
            _raw_message(
                _unix_ms("2026-08-08T12:00:00+00:00"), author_id="9", bot=True
            )
        ),
    ]
    meta = fetch_history.build_meta(
        guild_id="G1",
        guild_name="Smarter Dev",
        channel_id="C1",
        channel_name="general",
        day=date(2026, 8, 8),
        bot_user_id="B1",
        fetched_at="2026-08-17T00:00:00+00:00",
        records=records,
    )
    jsonl_path, meta_path = fetch_history.write_fixture(
        records, meta, data_dir=tmp_path
    )

    assert jsonl_path.name == "G1-general-2026-08-08.jsonl"
    assert meta_path.name == "G1-general-2026-08-08.meta.json"

    lines = jsonl_path.read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["author_id"] for line in lines] == ["1", "2", "9"]

    written_meta = json.loads(meta_path.read_text())
    assert written_meta == {
        "guild_id": "G1",
        "guild_name": "Smarter Dev",
        "channel_id": "C1",
        "channel_name": "general",
        "date": "2026-08-08",
        "fetched_at": "2026-08-17T00:00:00+00:00",
        "message_count": 3,
        "human_message_count": 2,
        "distinct_human_authors": 2,
        "bot_user_id": "B1",
    }


# --- guild and channel resolution -------------------------------------------


def _json_route_transport(routes: dict[str, object]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes[request.url.path])

    return httpx.MockTransport(handle)


async def test_resolve_guild_matches_name_case_insensitively():
    client = fetch_history.HistoryClient(
        bot_token="tok",
        transport=_json_route_transport(
            {
                "/api/v10/users/@me/guilds": [
                    {"id": "G1", "name": "Other Guild"},
                    {"id": "G2", "name": "Smarter Dev"},
                ]
            }
        ),
    )
    guild = await fetch_history.resolve_guild(
        client, guild_id=None, guild_name="smarter dev"
    )
    assert guild["id"] == "G2"


async def test_resolve_guild_error_lists_available_names():
    client = fetch_history.HistoryClient(
        bot_token="tok",
        transport=_json_route_transport(
            {"/api/v10/users/@me/guilds": [{"id": "G1", "name": "Other Guild"}]}
        ),
    )
    with pytest.raises(SystemExit, match="Other Guild"):
        await fetch_history.resolve_guild(
            client, guild_id=None, guild_name="Smarter Dev"
        )


async def test_resolve_channel_matches_text_channel_by_name():
    client = fetch_history.HistoryClient(
        bot_token="tok",
        transport=_json_route_transport(
            {
                "/api/v10/guilds/G1/channels": [
                    {"id": "C1", "name": "general", "type": 2},  # voice
                    {"id": "C2", "name": "general", "type": 0},
                    {"id": "C3", "name": "random", "type": 0},
                ]
            }
        ),
    )
    channel = await fetch_history.resolve_channel(
        client, "G1", channel_id=None, channel_name="general"
    )
    assert channel["id"] == "C2"


# --- rate limiting -----------------------------------------------------------


async def test_rate_limited_request_retries_after_retry_after():
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "message": "You are being rate limited.",
                    "retry_after": 0.01,
                    "global": False,
                },
            )
        return httpx.Response(200, json=[])

    client = fetch_history.HistoryClient(
        bot_token="tok", transport=httpx.MockTransport(handle)
    )
    page = await client.message_page("C")
    assert page == []
    assert len(calls) == 2


async def test_non_rate_limit_error_propagates_without_retry():
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, text="Forbidden")

    client = fetch_history.HistoryClient(
        bot_token="tok", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(DiscordRestError):
        await client.message_page("C")
    assert len(calls) == 1


def test_retry_after_seconds_parses_discord_body():
    error = DiscordRestError(
        'GET /channels/C/messages -> 429: {"message": "slow down", '
        '"retry_after": 2.5, "global": false}'
    )
    assert fetch_history.retry_after_seconds(error) == 2.5


def test_retry_after_seconds_falls_back_on_unparseable_body():
    error = DiscordRestError("GET /channels/C/messages -> 429: nope")
    assert (
        fetch_history.retry_after_seconds(error)
        == fetch_history.DEFAULT_RETRY_AFTER_SECONDS
    )
