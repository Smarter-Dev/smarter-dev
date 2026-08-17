# Stage 03 — Periodic-activation simulator, bot adapter interface, cost accounting

## Context (read first — none of this is in your memory)

Stages 1–2 (already merged) produced, under the gitignored dir
`scripts/proactive_eval/data/`:

- `<guild_id>-<channel>-<date>.jsonl` — one full UTC day of the Smarter Dev
  #general channel, one message per line, ascending. Fields: `id`, `timestamp`
  (ISO UTC), `author_id`, `author_name`, `author_display`, `is_bot`, `content`,
  `reply_to_id`, `mention_user_ids`, `mention_everyone`, `attachment_count`,
  `sticker_count`, `reaction_counts`, `message_type`.
- `….meta.json` — includes `channel_name`, `guild_name`, `bot_user_id`.
- `….labels.json` — ground-truth `directed_at` label per human message
  (stage 4 consumes this; this stage does not).

The product goal: a future **proactive chat bot** (not yet built) will wake on
a timer, review messages since its last wake, and either stay silent or send
responses — responding only to messages directed at anyone/at it, never to
messages directed at other users. Two things must come out of this stage:

1. A **replay harness** that simulates that periodic activation over the
   fixture day against a pluggable bot implementation, producing a run record
   stage 4 can score.
2. **Cost measurement**: real token usage and USD cost per activation and per
   day, because the owner wants to know how expensive "actually following the
   conversation" would be.

Relevant existing code to reuse:

- `smarter_dev/bot/agents/chat_agent.py` — `build_agent_model(model_id)` maps a
  wire id like `"openai/gpt-5.6-luna"` or `"gemini-3.5-flash-lite"` to a
  configured pydantic-ai `Model` (OpenRouter/Google/OpenAI providers, keys from
  env). Use it so `--model` accepts the same ids the bot uses.
- `scripts/eval_prices.py` — registers prices for models newer than the bundled
  `genai_prices` snapshot. Import it (like
  `scripts/two_stage_conversation_eval.py` does) before calling
  `genai_prices.calc_price`.
- `scripts/two_stage_conversation_eval.py` — reference for cost math with
  `genai_prices.Usage` / `calc_price` and for `.env` loading conventions.

Project rules: TDD; `uv run`; no `typing.Optional`/`Union`; fail fast;
descriptive names; pure functions over mutation; semgrep + gitleaks before
commit.

## Assumptions (recorded for reviewer correction)

- Default activation cadence: every 5 minutes (`--every 300` seconds,
  configurable). An activation with zero new messages is recorded as a
  free skip (a real deployment would short-circuit before any model call).
- The baseline adapter is a **placeholder** for the unbuilt bot: a single
  pydantic-ai agent call per activation, default model
  `gemini-3.5-flash-lite`. Its purpose is (a) proving the harness end-to-end
  and (b) producing a realistic cost floor. The real bot later implements the
  same adapter protocol.
- The bot's own responses are injected into subsequent activations' history
  (stamped at their activation time) so the simulation behaves like a bot that
  sees its own past messages; they are NOT appended to the fixture file.
- Context given per activation: the new messages since last activation plus
  the trailing 60 messages of history (fixture + injected bot responses,
  interleaved by timestamp). 60 ≈ what the real chat agent sees today.

## Deliverables

1. `scripts/proactive_eval/simulation.py` — pure core: dataclasses, windowing,
   history interleaving, run-record building, cost math.
2. `scripts/proactive_eval/adapters.py` — `ProactiveBotAdapter` protocol +
   `BaselineAdapter` + `SilentAdapter` (always returns no responses; useful for
   harness smoke tests and as a lower cost bound).
3. `scripts/proactive_eval/simulate.py` — CLI:

   ```
   uv run python -m scripts.proactive_eval.simulate scripts/proactive_eval/data/<fixture>.jsonl \
       [--every 300] [--model gemini-3.5-flash-lite] [--adapter baseline|silent] \
       [--history-size 60] [--out scripts/proactive_eval/data/runs/<auto>.json]
   ```

4. Tests in `tests/scripts/test_proactive_eval_simulation.py`.

## Adapter protocol (the contract the future real bot implements)

```python
@dataclass(frozen=True)
class ActivationContext:
    channel_name: str
    guild_name: str
    bot_user_id: str
    activated_at: datetime            # simulated wall clock (window end)
    history: list[FixtureMessage]     # trailing context before this window, incl. injected bot msgs
    new_messages: list[FixtureMessage]  # messages inside this window (the ones to consider)

@dataclass(frozen=True)
class ProposedResponse:
    reply_to_id: str | None   # fixture message id this responds to; None = standalone
    content: str

@dataclass(frozen=True)
class ActivationResult:
    responses: list[ProposedResponse]      # empty = stayed silent
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    model_id: str

class ProactiveBotAdapter(Protocol):
    async def activate(self, context: ActivationContext) -> ActivationResult: ...
```

`FixtureMessage` is a dataclass mirroring the JSONL schema plus
`injected_bot_response: bool`. Keep all of these in `simulation.py` /
`adapters.py` so stage 4 and the future bot import from one place.

## Simulation loop

- Windows: from the fixture's first message timestamp floored to the cadence
  boundary, step `--every` seconds, until past the last message. Each window's
  `new_messages` = fixture messages with `window_start <= ts < window_end`.
- Empty window → record `{"skipped": true}` activation, no adapter call.
- Non-empty window → build `ActivationContext` (history = last
  `--history-size` items of fixture-so-far + previously injected bot
  responses, merged by timestamp), await the adapter, inject its responses as
  bot-authored `FixtureMessage`s stamped `activated_at`.
- The loop is sequential by design (later activations depend on earlier
  injected responses). Print one progress line per non-empty activation.

## Baseline adapter

- pydantic-ai `Agent` with output type
  `{"responses": [{"reply_to_id": str | None, "content": str}]}` (a pydantic
  model; empty list allowed and expected most windows).
- System prompt (write it in-code; roughly): you are <bot name> in the
  <guild>/<channel> Discord channel; you wake every N minutes and review new
  messages; you may reply ONLY to messages addressed to the whole room or to
  you; never insert yourself into an exchange between specific people; when
  nothing warrants a reply, return no responses; at most 2 responses per wake.
- User prompt: rendered transcript — `HISTORY` block then `NEW MESSAGES` block
  with ids (same rendering style stage 2 used: `[id=…] display (reply to id=…): content`,
  bot messages marked).
- Usage extraction: `result.usage()` from pydantic-ai → map
  `input_tokens`/`output_tokens`/`cache_read_tokens` (default 0 when absent).
- Model via `build_agent_model(model_id)`; `.env` loaded at CLI start with
  `load_dotenv` like the other scripts.

## Run record (input to stage 4 — keep exact)

`scripts/proactive_eval/data/runs/<fixture-stem>.<adapter>.<model>.<every>s.json`
(runs/ lives inside the gitignored data dir):

```json
{
  "fixture": "<fixture filename>",
  "adapter": "baseline",
  "model_id": "gemini-3.5-flash-lite",
  "cadence_seconds": 300,
  "history_size": 60,
  "started_at": "…", "finished_at": "…",
  "activations": [
    {
      "index": 17,
      "window_start": "…", "window_end": "…",
      "skipped": false,
      "new_message_count": 4,
      "history_count": 60,
      "responses": [{"reply_to_id": "1403…", "content": "…"}],
      "input_tokens": 5200, "output_tokens": 40, "cache_read_tokens": 0,
      "cost_usd": 0.001694
    }
  ],
  "totals": {
    "activations": 288, "activations_with_messages": 121,
    "activations_with_responses": 9, "responses": 11,
    "input_tokens": 610000, "output_tokens": 4100, "cache_read_tokens": 0,
    "cost_usd": 0.21
  }
}
```

## Cost accounting and the report the owner asked for

Per-activation cost via `genai_prices.calc_price(Usage(...), model_ref=…,
provider_id=…)` after importing `scripts/eval_prices` (see
`two_stage_conversation_eval.py` for provider-id mapping). After the run,
print a **cost summary** block:

- total day cost, tokens in/out;
- mean/median/p95 input tokens per non-empty activation;
- projections: cost/day → cost/30-days at this cadence;
- naive cadence sensitivity: measured messages-per-window distribution re-bucketed
  at 2 min / 5 min / 15 min to show how activation count changes (state clearly
  that token cost per activation is measured only at the run cadence).

Also write the same summary into the run record under `"cost_summary"`.

## Tests (write first) — no network anywhere

1. Windowing: boundaries at cadence multiples, messages assigned to the right
   window, empty windows skipped.
2. History interleaving: injected bot responses appear in later contexts in
   timestamp order and are marked `injected_bot_response`.
3. Sequential dependency: with a scripted stub adapter, a response injected in
   window N is present in window N+1's history.
4. Usage → cost math with a fixed price table (monkeypatch or a tiny fake
   `calc_price`) and totals aggregation.
5. Run-record serialization matches the schema above (round-trip json).
6. `SilentAdapter` end-to-end over a small synthetic fixture: zero cost,
   correct activation counts.
7. Baseline adapter prompt rendering (transcript blocks, id lines) — pure
   function test; the agent call itself is exercised with pydantic-ai's
   `TestModel`/`FunctionModel` if convenient, otherwise stubbed at the adapter
   seam.

`uv run pytest tests/scripts/test_proactive_eval_simulation.py` must pass
(scope pytest to `tests/scripts/`; the bare full suite has known pre-existing
breakage).

## Acceptance

- A real run: `simulate` over the stage-1 fixture with the baseline adapter at
  default cadence completes, writes the run record, and prints the cost
  summary. Put the cost summary numbers in the completion report — that's the
  owner's "how expensive would this be" answer (first cut).
- A `--adapter silent` run costs $0 and records the same activation skeleton.
- Tests pass; semgrep + gitleaks clean.
