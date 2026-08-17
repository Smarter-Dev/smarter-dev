# Stage 04 — Judge scoring of a simulation run and the final eval report

## Context (read first — none of this is in your memory)

Stages 1–3 (already merged) built, all under `scripts/proactive_eval/`:

- `fetch_history.py` → fixture `data/<guild_id>-<channel>-<date>.jsonl` (one
  UTC day of Smarter Dev #general, one message per line; fields include `id`,
  `timestamp`, `author_display`, `is_bot`, `content`, `reply_to_id`,
  `message_type`) + `data/….meta.json` (has `bot_user_id`, `channel_name`).
- `label_day.py` → `data/….labels.json`: for every human message,
  `directed_at ∈ {other_user, anyone, bot, ambient}`, `target_user_id`,
  `ok_to_respond` (true iff `anyone`/`bot`), `reason`. Labels were produced by
  Claude Code run headless (`claude -p … --output-format json`) — "the coding
  agent as the judge". `label_day.py` contains a `run_judge(prompt, model)`
  subprocess wrapper — move/reuse it (see below).
- `simulate.py` → run record `data/runs/….json`: per activation the window,
  `responses: [{reply_to_id, content}]`, token usage, `cost_usd`; plus
  `totals` and `cost_summary`. `simulation.py` holds the shared dataclasses.

This final stage scores a run and produces the report a human reads:
**did the simulated bot respond only to messages directed at anyone/at it,
never to messages directed at other users — and what did the day cost?**

Project rules: TDD; `uv run`; no `typing.Optional`/`Union`; fail fast;
descriptive names; semgrep + gitleaks before committing.

## Assumptions (recorded for reviewer correction)

- A response's final verdict combines two independent checks; it is
  **appropriate** only if both pass:
  1. **Label check (deterministic):** `reply_to_id` present → its label must
     have `ok_to_respond == true`. A standalone response (`reply_to_id` null)
     has no label check (judge-only).
  2. **Judge check (Claude Code):** given surrounding transcript, the judge
     may fail a response even when the label allows it (barging into a lull in
     someone else's exchange, redundant answer, off-tone).
- Missed opportunities are **informational only**: the goal statement is about
  not responding to other-directed messages (precision), so misses (messages
  labeled `bot`-directed with no response, and unanswered `anyone` messages)
  are reported but don't fail anything.
- Judge model default `claude-sonnet-5`, same CLI contract as stage 2.
- Reports are content-bearing (they quote member messages) → they go in the
  gitignored `scripts/proactive_eval/data/reports/` dir, not the repo's
  tracked `reports/`.

## Deliverables

1. Refactor: move `run_judge` (the `claude -p` subprocess wrapper) from
   `label_day.py` into a new `scripts/proactive_eval/judge.py`; `label_day.py`
   imports it from there. Behavior unchanged.
2. `scripts/proactive_eval/scoring.py` — pure logic: verdict combination,
   metrics, judge-prompt rendering, judge-output parsing, report rendering.
3. `scripts/proactive_eval/score_run.py` — CLI:

   ```
   uv run python -m scripts.proactive_eval.score_run scripts/proactive_eval/data/runs/<run>.json \
       [--judge-model claude-sonnet-5] [--force]
   ```

   Locates the fixture and labels files via the run record's `fixture` field
   (fail fast if the labels file is missing — tell the user to run
   `label_day` first).
4. Tests in `tests/scripts/test_proactive_eval_scoring.py`.

## Judge pass over responses

For each response in the run (skip skipped/empty activations):

- Render a prompt containing: the eval's rule (respond only to messages
  directed at anyone or at the bot, never to messages directed at other
  users, don't be annoying or redundant); the 30 fixture messages surrounding
  the responded-to point (15 before the trigger — or before the activation
  window for standalone responses — through 15 after, so the judge sees what
  the human conversation actually did next); the ground-truth label of the
  trigger message when present; and the bot's response content.
- Ask for **only** a fenced JSON object:
  `{"appropriate": bool, "severity": "fine" | "minor" | "bad", "reason": "one sentence"}`
  — `bad` means it inserted itself into another person's exchange; `minor`
  means allowed target but poor judgment.
- Invoke via `judge.run_judge`; one call per response (a busy day should
  still be at most a few dozen responses). Cache each verdict under
  `data/.score_cache/<run-stem>/response-<activation>-<n>.json`; `--force`
  clears. Fail fast on unparseable output.

## Deterministic metrics (computed in `scoring.py`, no judge needed)

- `responses_total`, `responses_to_labeled`, `responses_standalone`
- `label_violations` — responses whose trigger label is `other_user` or
  `ambient` (list each with trigger id and label reason)
- `judge_failures` — responses the judge marked inappropriate
- `appropriate_responses` — passed both checks
- `response_precision` = appropriate / total (the headline number)
- Informational: `bot_directed_missed` (messages labeled `bot` with no
  response in any activation), `open_messages_answered` vs `anyone`-labeled
  total, responses per hour.
- Cost block copied from the run record's `cost_summary`.

## Report

Write `data/reports/<run-stem>.md` and print the summary table to stdout:

1. Header: fixture date/channel, adapter, model, cadence, judge model.
2. Headline table: precision, violations, judge failures, misses, day cost,
   projected 30-day cost.
3. **Violations** section: for each label violation and each judge `bad`, a
   short transcript excerpt (5 messages around the trigger), the bot's
   response, the label, the judge's reason.
4. **Judge disagreements**: responses where label said ok but judge failed
   them (these are the interesting cases for tuning the future bot).
5. Misses section (informational).
6. Cost section: per-activation token distribution and projections, verbatim
   from `cost_summary`.

## Tests (write first) — judge stubbed everywhere, no subprocess in tests

1. Verdict combination: label ok + judge ok → appropriate; each failure path;
   standalone response = judge-only.
2. Metrics math over a synthetic run + labels (violations, precision, misses).
3. Judge prompt rendering: includes rule text, ±15-message excerpt bounds
   respected at day edges, label included when present.
4. Judge output parsing: valid, missing key, bad severity → error.
5. Cache/resume behavior with a stub judge counting invocations.
6. Report rendering: sections present, violation excerpts show the trigger.
7. `run_judge` import move: `label_day` still imports and works (existing
   stage-2 tests keep passing unmodified except the import path if they
   patched it).

`uv run pytest tests/scripts/` must pass (the bare full repo suite has known
pre-existing breakage unrelated to this work).

## Acceptance

- End-to-end on real data: score the stage-3 baseline run; report file
  written; stdout shows the headline table. Quote the headline table (and 2–3
  violation examples if any) in the completion report — together with stage
  3's cost summary this answers both of the owner's questions: can a bot
  respond only to anyone/bot-directed messages, and what does following the
  conversation cost.
- Rerunning with the cache warm makes zero judge calls.
- Tests pass; semgrep + gitleaks clean.
