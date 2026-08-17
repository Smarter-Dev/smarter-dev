# Stage 01 — Discord history fetcher: find the busiest day and pull it

## Context (read first — none of this is in your memory)

This is stage 1 of 4 building an eval for a **not-yet-built proactive chat bot**.
The bot will periodically wake up, review new messages in the Smarter Dev
Discord #general channel, and respond only to messages directed at *anyone* or
at *it* — never to messages directed at other specific users. The eval replays a
real day of chat through a simulated periodic-activation loop (stage 3), labels
ground truth and judges responses with Claude Code as the judge (stages 2 and
4), and measures token cost per activation.

This stage builds the data supply: a CLI that (a) scans recent #general history
to find the busiest day and (b) pulls that full day into a local fixture file.
Today (2026-08-15) may have been slow, so busy-day selection is a hard
requirement, not a nicety.

Relevant existing code:

- `smarter_dev/web/discord_rest.py` — `DiscordBotClient`, a minimal bot-token
  REST caller over httpx. It accepts an injectable `transport`
  (httpx.MockTransport in tests) and `api_base`. **Reuse it** as the HTTP layer;
  do not write a second raw Discord client.
- `scripts/` — existing eval scripts (`chat_eval.py`, `two_stage_conversation_eval.py`)
  show local conventions: `#!/usr/bin/env python` scripts run with
  `uv run python`, `load_dotenv(REPO_ROOT / ".env")`, argparse.
- `tests/scripts/` — pytest tests importing from the `scripts.` package
  (see `tests/scripts/test_two_stage_conversation_eval.py`).
- `.env` (never committed) contains `PROD_DISCORD_BOT_TOKEN` — the production
  bot's token, which has read access to the Smarter Dev guild. `DISCORD_BOT_TOKEN`
  is the local/dev bot.

Project rules that apply (from the repo owner's standing instructions): TDD —
write tests first for all pure logic; run python with `uv`; no
`typing.Optional`/`typing.Union` (use `X | None`); fail fast — only catch
specific exceptions to add context and re-raise; descriptive names; run
`semgrep` and `gitleaks` before committing.

## Assumptions (recorded for reviewer correction)

- "General chat" means the text channel named `general` in the production
  Smarter Dev guild. Both are discovered by name at runtime; `--guild-id` /
  `--channel-id` flags override.
- Busy-day lookback default is 45 days.
- Days are bucketed in UTC.
- Fixture files contain real member messages, so they are **gitignored** and
  never committed. Only the tooling is committed.
- The token env var defaults to `PROD_DISCORD_BOT_TOKEN` (a `--token-env` flag
  selects another).

## Deliverables

1. New package `scripts/proactive_eval/` with `__init__.py`.
2. `scripts/proactive_eval/fetch_history.py` — CLI with two subcommands, `scan`
   and `pull`.
3. `.gitignore` entry: `scripts/proactive_eval/data/`.
4. Tests in `tests/scripts/test_proactive_eval_fetch_history.py` (create
   `tests/scripts/` files following existing conventions).

## CLI behavior

```
uv run python -m scripts.proactive_eval.fetch_history scan  [--days 45] [--channel general] [--guild-name "Smarter Dev"] [--guild-id ID] [--channel-id ID] [--token-env PROD_DISCORD_BOT_TOKEN]
uv run python -m scripts.proactive_eval.fetch_history pull --date 2026-08-08 [same flags]
```

### `scan`

- Resolve guild: `GET /users/@me/guilds`, match `--guild-name`
  (case-insensitive) unless `--guild-id` given. Fail fast with a clear error
  listing available guild names on no match.
- Resolve channel: `GET /guilds/{guild_id}/channels`, match text channel by
  name unless `--channel-id` given.
- Page backwards through `GET /channels/{channel_id}/messages?limit=100&before=<id>`
  starting from now until messages older than `now - days`. Only id, timestamp,
  author id and `author.bot` are needed for counting.
- Bucket by UTC date. Per day compute: total messages, human (non-bot)
  messages, distinct human authors, and an engagement score =
  `human_messages * distinct_human_authors`.
- Print a table (one row per day, newest first) and end with a recommendation
  line naming the highest-scoring day and the exact `pull` command to run.

### `pull`

- Same guild/channel resolution.
- Fetch every message in `[date 00:00 UTC, date+1 00:00 UTC)` ascending via
  `after=<snowflake>` pagination; stop at the first message past day end.
- Snowflake math (needed for both directions):
  `snowflake = (unix_ms - 1420070400000) << 22`, and
  `timestamp_ms = (snowflake >> 22) + 1420070400000`. Put these in pure
  functions.
- Write two files under `scripts/proactive_eval/data/`:
  - `<guild_id>-<channel>-<date>.jsonl` — one message per line, ascending
    timestamp, schema below.
  - `<guild_id>-<channel>-<date>.meta.json` — `guild_id`, `guild_name`,
    `channel_id`, `channel_name`, `date`, `fetched_at` (ISO UTC), `message_count`,
    `human_message_count`, `distinct_human_authors`, `bot_user_id` (from
    `GET /users/@me` — stage 3 needs to know which author is "the bot").

### Message JSONL schema (stages 2–4 all consume this — keep it exact)

```json
{
  "id": "1403…",
  "timestamp": "2026-08-08T14:03:22.123000+00:00",
  "author_id": "266…",
  "author_name": "zzmmrmn",
  "author_display": "zech",
  "is_bot": false,
  "content": "does anyone know why …",
  "reply_to_id": null,
  "mention_user_ids": ["…"],
  "mention_everyone": false,
  "attachment_count": 0,
  "sticker_count": 0,
  "reaction_counts": {"👍": 2},
  "message_type": 0
}
```

Notes: `reply_to_id` comes from `message_reference.message_id` when
`referenced_message`/`message_reference` is present and `type` is 19 (reply);
otherwise null. `author_display` = `member.nick` if the messages payload
carries it, else `author.global_name`, else `author.username`. Keep
`message_type` raw (0 = default, 19 = reply, others are system messages —
downstream stages filter on it).

## Implementation notes

- Build a small `HistoryClient(DiscordBotClient)` subclass (or use
  `DiscordBotClient` directly) so `transport` and `api_base` stay injectable —
  that is the test seam.
- Rate limiting: on HTTP 429, `DiscordBotClient._request` raises. Catch **only**
  the 429 case (`error.status_code == 429`), sleep the `retry_after` from the
  response body, retry the page. Any other error propagates. A polite fixed
  delay (e.g. 0.25 s) between pages is fine and keeps the scan well under
  Discord's limits; a 45-day scan of a busy channel is a few hundred requests.
- No database involvement anywhere in this eval. Do not touch the app's DB.
- Print progress (pages fetched, oldest timestamp reached) so a multi-minute
  scan is visibly alive.

## Tests (write first)

All network via `httpx.MockTransport` handlers that serve canned Discord JSON;
no live calls in tests.

1. Snowflake ↔ timestamp round-trips and a known fixed vector.
2. Backward pagination stops once messages are older than the lookback bound
   and requests use decreasing `before` ids.
3. Forward (`after`) pagination for `pull` collects exactly the messages inside
   the UTC day and stops past day end.
4. Day bucketing and engagement scoring: bots excluded from human counts,
   distinct-author math, recommendation picks max score.
5. JSONL serialization: reply extraction, display-name fallback chain,
   reaction counts, meta sidecar contents.
6. 429 handling: one retry after `retry_after`, other statuses raise.

`uv run pytest tests/scripts/test_proactive_eval_fetch_history.py` must pass.
Note: the full bare `pytest` suite has known pre-existing breakage (fastapi
import + a flaky perf test) — scope your runs to your own test file plus
`tests/scripts/`.

## Acceptance

- `scan` against the real guild (run it once manually with
  `PROD_DISCORD_BOT_TOKEN`; the human reviewer expects this smoke test in the
  completion report) prints the per-day table and a recommendation.
- `pull` writes fixture + meta for the recommended day; line count matches the
  meta count; file is under the gitignored data dir (`git status` clean).
- Tests pass; semgrep and gitleaks clean.
