# Stage 04 — Apply the override + enforce token budgets in the chat runtime

**Stable id:** `enforcement-budgets`

## Outcome
Make the stored per-channel override actually drive inference: when a channel has
an override, the chat agent uses that model (via the stage-01 router), and daily/
hourly token budgets are enforced (0 = unlimited). This completes the feature.

## Prior state (already committed)
- Stage 01: `model_router.build_model_for(CatalogModel)` +
  `chat_agent.build_agent_model(model_id)`; `model_catalog.get_model(key)`.
- Stage 02: `ModelOverrideService` (`bot.d["model_override_service"]`) →
  `get_override(guild, channel)` returns model_key + daily/hourly budgets (or None).
- Stage 03: admins can set/clear the override via `/setmodel`.

## Context you need (read before starting)
- **Where inference happens**: `smarter_dev/bot/services/chat_engine.py`
  `ChannelEngine._run_once()` (~lines 339–580) builds the input and calls
  `get_chat_agent()` then `await agent.run(...)`; it already extracts token usage
  (`result.usage()` → input/output tokens, `_extract_tokens()` ~lines 878–881) and
  persists the turn (`persist_turn(...)` with `chat_tokens_input`/`chat_tokens_output`).
  This is the single place to (a) select the override model and (b) enforce +
  increment budgets.
- **Agent construction**: `smarter_dev/bot/agents/chat_agent.py` — `get_chat_agent()`
  builds a **singleton** keyed off `CHAT_AGENT_MODEL`/`DEFAULT_MODEL`. A per-channel
  override means you can no longer use a single global agent. Refactor to a
  **per-model cache**: `get_chat_agent(model_id: str | None = None)` returns/creates
  an `Agent` for that model (build via `build_agent_model`), caching by resolved
  model id (`dict[str, Agent]`). `model_id=None` keeps today's default. Preserve all
  current agent config (output_type, tools, system prompt, model_settings,
  history_processors). Update the compaction agent similarly only if it must match
  the override model — otherwise leave compaction on its existing model (record the
  decision).
- **Rate-limit pattern to copy**: `smarter_dev/web/image_quota.py` — Redis
  fixed-window (`IMAGES_PER_HOUR`, `WINDOW_SECONDS=3600`, key `imgquota:{guild_id}`,
  `peek`/`reserve`/`release`). Also `smarter_dev/web/handler_caps.py`. The bot has
  Redis via its redis manager (see `tests/conftest.py` `redis_manager` /
  `mock_redis_manager` and how services get Redis). Enforcement here runs bot-side
  inside `chat_engine`, so use the bot's Redis directly rather than a new API round
  trip per turn.
- Global rules: fail fast; specific exceptions only; no inline imports; `X | None`;
  builtins for annotations; pure functions where possible (no mutating passed-in
  args); TDD.

## What to build

### 1. Per-channel token budget helper (Redis fixed-window)
Create `smarter_dev/bot/services/channel_token_budget.py`:
- Two fixed windows per channel: hour (3600s) and day (86400s), aligned to wall
  clock (window index = epoch // window_seconds), keys:
  `modelbudget:{channel_id}:hour:{window_index}` and `:day:{window_index}`
  (mirror `image_quota` key style; set TTL = window length on first increment).
- `async def is_over_budget(redis, channel_id, daily_budget, hourly_budget) -> bool`
  — returns True if either budget is non-zero AND the current window's consumed
  tokens ≥ that budget. `0` budget = unlimited (never blocks on that window).
- `async def add_usage(redis, channel_id, tokens: int) -> None` — increment both
  windows by `tokens` (INCRBY), set expiries. Pure w.r.t. inputs; only touches
  Redis.
- Keep it dependency-light and testable with a fake/mock Redis.

### 2. Wire override selection into `chat_engine._run_once()`
At the point the engine is about to run a turn for a channel:
1. `override = await model_override_service.get_override(guild_id, channel_id)`
   (reach the service via the bot; the engine already has channel/guild ids and a
   handle to bot services — follow how it currently gets other services).
2. Resolve model: if override, `catalog = get_model(override.model_key)`; use
   `catalog.model_id` → `agent = get_chat_agent(catalog.model_id)`. Else the
   default agent (`get_chat_agent()`). If `override.model_key` is somehow unknown to
   the catalog (stale), fall back to default and log a warning (fail soft here — a
   bad stored key must not break chat).
3. **Budget check (before running)**: if override present and
   `await is_over_budget(redis, channel_id, override.daily_token_budget, override.hourly_token_budget)`
   → skip the turn (do not call the model). Match the engine's existing
   "don't respond this turn" path (there is a `MAX_NO_RESPONSE_TURNS`/skip concept
   already) so state stays consistent. Optionally post a throttled ephemeral/one-time
   notice to the channel that the budget is exhausted — keep it minimal and rate
   limited so it can't spam. Record the decision on whether to notify.
4. **After a successful run**: `await add_usage(redis, channel_id, input_tokens + output_tokens)`
   using the tokens the engine already extracts. Do this for override channels;
   for non-override channels usage tracking is optional (skip to avoid overhead, or
   track anyway — record the decision). Also count compaction tokens toward the
   channel budget only if compaction used the override model (be consistent with the
   stage-1 compaction decision).

Keep these additions surgical; do not restructure the engine loop beyond what's
needed. Selecting the model and the pre/post budget hooks should be small, clearly
named helpers.

### 3. No default-behavior regression
Channels **without** an override must behave exactly as before: default model,
no budget checks, singleton-equivalent agent (the per-model cache returns the same
default agent instance for the default id).

## Tests (TDD — write first)
- `tests/bot/services/test_channel_token_budget.py` (mock/fake Redis):
  - `add_usage` increments both hour and day windows; sets TTLs.
  - `is_over_budget`: `0` budgets never block; hourly budget blocks when hour usage
    ≥ budget even if day is under; day budget blocks when day usage ≥ budget;
    under-budget → False.
- `tests/bot/agents/test_chat_agent_override.py`:
  - `get_chat_agent("gemini-3.1-flash-lite")` and `get_chat_agent(None)` return
    agents; distinct model ids yield distinct cached agents; same id returns the
    same cached instance.
- `tests/bot/services/test_chat_engine_override.py` (patch
  `model_override_service`, `get_chat_agent`/`build_agent_model`, and the Redis
  budget helper; the engine's `agent.run` mocked to return a fake result with a
  known `usage()`):
  - override present → engine builds the agent for the override model id.
  - over-budget → `agent.run` NOT called (turn skipped).
  - under-budget → `agent.run` called, then `add_usage` called with
    `input+output` tokens.
  - no override → default agent, no budget check, `add_usage` not called (or the
    chosen default behavior).
  - unknown stored model_key → falls back to default, logs warning, does not crash.
- Full suite: `uv run pytest` (LLM tests remain excluded by default; do NOT add
  real-model calls to these — everything is mocked).

## Acceptance criteria
- A channel with an override set via `/setmodel` uses that model for chat turns.
- Daily/hourly token budgets are enforced with `0 == unlimited`; exhausted budget
  stops further model calls in that window; the window resets on the next
  hour/day.
- Channels without an override are unchanged.
- `uv run pytest` green. Run `semgrep` + `gitleaks` before committing.

## Notes / decisions to record in the commit
- Whether/how you notify a channel when the budget is exhausted (and its rate
  limit).
- Whether non-override channels get usage tracking.
- Whether compaction tokens count toward the budget and which model compaction
  uses under an override.
- End-to-end sanity: consider driving the `/verify` or `/run` skill against a test
  guild if credentials are available; otherwise the mocked engine tests are the
  verification of record.
