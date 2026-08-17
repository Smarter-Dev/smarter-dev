# Stage 02 — Ground-truth labeling with Claude Code as the judge

## Context (read first — none of this is in your memory)

Stage 1 (already merged) added `scripts/proactive_eval/fetch_history.py`, which
pulls one full UTC day of the Smarter Dev #general channel into
`scripts/proactive_eval/data/<guild_id>-<channel>-<date>.jsonl` (one message
per line; fields: `id`, `timestamp`, `author_id`, `author_name`,
`author_display`, `is_bot`, `content`, `reply_to_id`, `mention_user_ids`,
`mention_everyone`, `attachment_count`, `sticker_count`, `reaction_counts`,
`message_type`) plus a `.meta.json` sidecar carrying `bot_user_id`. Data files
are gitignored — real member chat is never committed.

The overall eval (stages 3–4) will simulate a proactive chat bot that wakes
periodically and must respond **only** to messages directed at *anyone* (open
questions/statements to the room) or at *the bot itself* — never to messages
directed at another specific user. To score that, every human message in the
day needs a ground-truth label saying who it was directed at.

The labeler is **Claude Code run headless** — the repo owner explicitly wants
"the coding agent as the judge". Invocation:

```
claude -p "<prompt>" --output-format json --model claude-sonnet-5
```

`--output-format json` prints a single JSON object to stdout whose `result`
field is the model's text and which carries usage metadata (`total_cost_usd`,
`usage`). The judge runs on the owner's Claude subscription, so judge cost is
not the eval's cost concern (that is the simulated bot's spend, stage 3), but
record what the CLI reports anyway.

Project rules that apply: TDD; `uv run` for python; no `typing.Optional`/
`Union`; fail fast (catch specific exceptions only, re-raise with context);
descriptive names; semgrep + gitleaks before committing.

## Assumptions (recorded for reviewer correction)

- Judge model pinned to `claude-sonnet-5` by default (`--judge-model` flag
  overrides). Sonnet is accurate enough for directedness classification and
  cheap/fast enough to label several hundred messages in chunks.
- The `claude` CLI is installed and authenticated on the machine running the
  script; the script fails fast with a clear message if `claude` is not on
  PATH or exits nonzero.
- Bot-authored messages and Discord system messages (`message_type` not in
  {0, 19}) are not labeled — the bot-under-test is never scored for replying
  to itself or to join notices; those messages still appear as context.

## Deliverables

1. `scripts/proactive_eval/labels.py` — label schema + chunking + prompt
   rendering + merge logic (pure, import-safe, no subprocess at import time).
2. `scripts/proactive_eval/label_day.py` — CLI that drives the judge:

   ```
   uv run python -m scripts.proactive_eval.label_day scripts/proactive_eval/data/<fixture>.jsonl \
       [--judge-model claude-sonnet-5] [--chunk-size 60] [--context-size 20] [--force]
   ```

3. Tests in `tests/scripts/test_proactive_eval_labels.py`.

## Label schema

Output file: `scripts/proactive_eval/data/<fixture-stem>.labels.json` (same
gitignored data dir):

```json
{
  "fixture": "<fixture filename>",
  "judge_model": "claude-sonnet-5",
  "labeled_at": "2026-08-15T…+00:00",
  "judge_reported_cost_usd": 0.42,
  "labels": {
    "<message_id>": {
      "directed_at": "other_user" | "anyone" | "bot" | "ambient",
      "target_user_id": "266…" | null,
      "ok_to_respond": true | false,
      "reason": "one short sentence"
    }
  }
}
```

Category definitions (put these, with examples, verbatim in the judge prompt):

- `other_user` — addressed to a specific person who is not the bot: a Discord
  reply to someone's message continuing that exchange, an @mention of a
  specific user, an answer inside an ongoing back-and-forth between two people,
  a message using someone's name ("bob did you push it?").
- `anyone` — an open bid to the room: questions, help requests, opinions or
  announcements not aimed at one person ("does anyone know…", "TIL…", showing
  off a project).
- `bot` — addressed to the bot: mentions of `bot_user_id` (from the meta
  sidecar), replies to a bot message, or the bot addressed by name.
- `ambient` — not a conversational bid at all: bare emoji/reactions-as-text,
  slash-command invocations, "lol", link drops with no ask, greetings into the
  void that a bot butting in on would be odd.

`ok_to_respond` is derived deterministically in code, not by the judge:
`directed_at in {"anyone", "bot"}`. The judge only outputs `directed_at`,
`target_user_id`, `reason`.

## Chunking and prompting

- Split labelable messages into chunks of `--chunk-size` (default 60), each
  prefixed by the `--context-size` (default 20) messages immediately before
  the chunk (any author, including bot/system) rendered as context.
- Render the transcript with stable single-letter speaker tags plus real
  display names and message ids, replies marked, bot messages marked `[BOT]`.
  Example line: `[id=1403… ] alice (reply to id=1402…): sure, pushing now`.
- The prompt instructs: label ONLY the ids listed in a `LABEL THESE` section
  (context ids must not appear in output), definitions as above, output **only**
  a fenced JSON object mapping message id → {directed_at, target_user_id,
  reason}. No prose.
- Parsing: extract the first fenced JSON block from the CLI `result` field;
  `json.loads`; validate every requested id is present and every value's
  `directed_at` is one of the four categories. A missing/extra id or invalid
  category fails that chunk with a clear error (fail fast — no silent
  best-effort merge).

## Resumability

- Each chunk's raw judge output is cached at
  `scripts/proactive_eval/data/.label_cache/<fixture-stem>/chunk-<NN>.json`
  keyed by chunk index; on rerun, cached chunks are loaded instead of
  re-invoking the judge. `--force` clears the cache for the fixture. The cache
  dir is inside the already-gitignored data dir.
- After all chunks succeed, merge into the labels file and print a category
  histogram (count per `directed_at`, % ok_to_respond) so the human can sanity
  check the distribution.

## Judge invocation seam

Wrap the subprocess call in one function, e.g.
`run_judge(prompt: str, model: str) -> JudgeReply` in `label_day.py`, where
`JudgeReply` carries `result_text`, `cost_usd`, `raw`. It uses
`subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--model", model], …)`
with a generous timeout (300 s), checks the exit code, and json-parses stdout.
Everything else takes the function as a parameter so tests inject a stub —
tests never launch the real CLI.

## Tests (write first)

1. Chunking: correct sizes, context windows overlap correctly at boundaries,
   bot/system messages excluded from labelable set but present as context.
2. Prompt rendering: contains all labelable ids in `LABEL THESE`, context ids
   only in the transcript, category definitions present.
3. Parsing: fenced JSON extracted; extra id, missing id, bad category each
   raise; valid payload merges.
4. `ok_to_respond` derivation for all four categories.
5. Resume: with two chunk caches present and one absent, only the absent chunk
   invokes the (stub) judge; `--force` invokes all.
6. Merge + histogram output shape.

`uv run pytest tests/scripts/test_proactive_eval_labels.py` must pass. (The
bare full suite has known pre-existing breakage; scope runs to `tests/scripts/`.)

## Acceptance

- Running `label_day` against the stage-1 fixture produces a labels file
  covering every human default/reply message, with a printed histogram.
  Include the histogram in the completion report — the human reviewer will
  eyeball a handful of labels against the raw day.
- Interrupting mid-run and rerunning re-uses cached chunks.
- Tests pass; semgrep + gitleaks clean.
