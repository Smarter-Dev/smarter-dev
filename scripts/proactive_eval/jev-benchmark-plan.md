# Jev versus GLM 5.3 Flash benchmark plan

Status: tooling, policy, private fixtures, and tuning/held-out samples are
frozen. `scripts/proactive_eval/data/` remains intentionally gitignored. The
24-case Jev pilot and bounded GLM diagnostics are complete; held-out remains
untouched and the comparative evaluation is explicitly incomplete. The full
paid benchmark is paused. Production remains `z-ai/glm-5.3-flash`. The frozen
Jev settings, completed tuning-label audit, absolute held-out gates, and review
limitations are recorded in
[jev-review-readiness.md](jev-review-readiness.md).

Workspace preflight on 2026-09-20 found the supplied credential under the
legacy `JEV_API_KEY` name. The integration accepts that alias in memory while
preferring the SDK-native `TYPESAFE_API_KEY`; it never rewrites the `.env`.
A one-request synthetic smoke resolved `jev-latest` to `jev-1.13.0`, returned a
typed decision, and recorded usage. The private adjudicated fixtures and a
GLM-only `LITELLM_ENDPOINT`/`LITELLM_API_KEY` route were subsequently supplied;
credential values remain unprinted and unmodified.

Use the paid-gated two-call smoke before touching the private dataset. It is a
dry run without `--confirm-paid`, and a single `--model` limits it to one call:

```bash
uv run python -m scripts.proactive_eval.smoke_models
uv run python -m scripts.proactive_eval.smoke_models --confirm-paid
```

## Dataset and split

Use the existing `fetch_history.py`, `label_day.py`, replay, and scoring paths.
Select at least 12 whole UTC channel-days from real guild history: six tuning
and six held out. Split by channel-day (and keep adjacent days from one active
conversation on the same side) so a conversation never leaks across splits.
Target at least 240 non-empty two-pass burst windows, approximately 120 per
split.

The set must include direct mentions/replies, name-only bot addressing,
ambient discussion, messages directed to other users, useful open intervention
opportunities, explicit watch-criteria matches, and follow-ups. As collection
guidance, seek at least 25 direct-engagement windows, 50 ambient windows, 50
other-user exchanges, 35 useful-intervention windows, and 20 follow-ups. These
can overlap where the policy allows.

Keep fixture JSONL, sidecars, manifests, and raw run records under the existing
private `data/` tree. Only aggregate metrics and redacted disagreements belong
in a publishable report.

The old response labels remain useful for end-to-end scoring but are not wake
labels. Apply [wake-policy-v1.md](wake-policy-v1.md) independently and blindly,
then freeze every fixture, metadata, response-label, and wake-label hash:

```bash
uv run python -m scripts.proactive_eval.benchmark_manifest freeze \
  --tuning scripts/proactive_eval/data/<tuning-day>.jsonl \
  --heldout scripts/proactive_eval/data/<heldout-day>.jsonl

uv run python -m scripts.proactive_eval.benchmark_manifest check \
  scripts/proactive_eval/data/benchmark-manifest.json --full
```

Repeat `--tuning` and `--heldout` for every channel-day. `check --full` refuses
fewer than six days per split, fewer than 240 windows, changed hashes, incomplete
wake labels, cross-split channel-day duplicates, or missing TypeSafe price
assumptions.

The frozen 120-case tuning sample has now received a complete source-context
consistency audit: all 8 required and all 110 not-required windows were
confirmed with zero changes. Its two null-target windows are bot-only and have
no labelable human message, so they are unscored rather than semantically
ambiguous. The private aggregate audit binds the manifest and ordered case-ID
hashes without copying message content. Because this completion pass occurred
after tuning outputs existed and was not performed by a new independent human,
that limitation must accompany any result.

## Fixed models and execution

- Jev candidate: exploration resolved `typesafe:jev-latest` to `jev-1.13.0`.
  The first held-out run is frozen to `typesafe:jev-1.13.0`; it must not use the
  moving alias.
- Baseline watcher: `z-ai/glm-5.3-flash`; verify this exact ID at the configured
  LiteLLM/OpenRouter route immediately before the run.
- Skim: `z-ai/glm-5.3-flash` for both candidates. Jev never generates text.
- Responder: `gemini-3.8-flash` for both end-to-end arms.
- Concurrency: one. Provider cache behavior is unchanged and recorded; no local
  response cache. Alternate candidate order within each pair.
- Timeout: 30 seconds. The first classifier-only held-out pass is frozen to a
  0.5 Boolean threshold, 0.0 minimum-confidence threshold, zero retries, and no
  fallback; it records confidence for analysis. Production Jev configuration
  currently falls back to the separately configured skim/GLM model after
  timeout, API error, or confidence below 0.20; that uncalibrated global
  minimum-field threshold and the added call's latency/cost are explicit
  rollout limitations.

### Jev-only tuning diagnostic

The small pilot is deliberately stratified and must not be interpreted as a
representative prevalence sample or a comparison with GLM. It uses 24 distinct
windows from the already-frozen tuning sample: all eight required wakes, 14
deterministic negatives balanced across channels/categories, and both
ambiguous windows. `prepare` writes the ignored selection artifact before any
prediction. `run` verifies that artifact and its source-manifest hash, pins the
smoke-tested `jev-1.13.0`, disables fallback and validation retries, attempts
each case once, and stops at the first error or the $1 ceiling.

```bash
.venv/bin/python -m scripts.proactive_eval.jev_pilot prepare
.venv/bin/python -m scripts.proactive_eval.jev_pilot run --confirm-paid
```

The private result is written to
`scripts/proactive_eval/data/runs/jev-pilot.json`; the aggregate diagnostic
report is in `scripts/proactive_eval/jev-pilot-report.md`.

The original GLM-only diagnostic was run with:

```bash
.venv/bin/python -m scripts.proactive_eval.glm_pilot --confirm-paid
```

The GLM pilot reads only `LITELLM_ENDPOINT` and `LITELLM_API_KEY`, requires the
approved `llmproxy.zech.sh` HTTPS host, requests only `glm-5.3-flash`, bounds
each completion to 512 tokens, disables client/application retries and provider
fallbacks, and applies a superseding $5 hard total cap. Its preserved baseline
was completed in two stages after the first invocation stopped on an error;
this contradicted the stated stop rule even though no case was retried. The
runner now refuses all overwrite/resume attempts and stops on its first error.
The baseline is retained only as a diagnostic with that protocol deviation.

### Compact GLM repair diagnostic

Offline preparation uses the same four atomic Boolean judgments as Jev, native
strict JSON schema, and the same deterministic judgment-to-wake/brief policy.
It selects two required and two negative cases from the frozen 24-case tuning
pilot, leaves held-out unchanged, and writes an ignored immutable plan:

```bash
.venv/bin/python -m scripts.proactive_eval.glm_compact_pilot prepare
```

The separately authorized run used `run --confirm-paid`, at most four attempts,
zero retries or provider fallback, low reasoning effort, a 512-token completion
ceiling inclusive of reasoning tokens, stop on first error, no resume/overwrite,
and a $0.01 hard cap. The conservative prepared maximum was $0.003693425. It
persisted only safe structural diagnostics and never raw model output, error
text, or bodies.
Prepared plan SHA-256:
`682afad09da69269abb2df3f632034951c56cb86829a13db5dea266ee50e67a1`.

The first required-wake case completed with valid schema, exact field-level and
wake agreement with Jev, and 80 reasoning tokens inside 126 total completion
tokens. The second required-wake case returned HTTP 429, and the runner stopped
without retrying or reaching the two negatives. Repair-run budget accounting
was $0.0012395; cumulative conservative GLM accounting including the prior
diagnostic is $0.01432133 against the $5 ceiling. Details are in
[glm-compact-pilot-report.md](glm-compact-pilot-report.md). No restart is
authorized, and the full benchmark remains paused.

A new explicitly versioned cooldown trial later referenced—not resumed—the
stopped result. `glm-compact-paced-v2` used the prior-429 required case plus the
two unattempted negatives, with the unchanged contract/settings, at least 60
seconds between requests, an exact safe response-header allowlist, a three-call
maximum, and a $0.01 sub-cap. Two calls succeeded; the third returned HTTP 429
without `Retry-After`, reset/remaining, or request-ID headers, so the trial
stopped. Its report is
[glm-compact-paced-v2-report.md](glm-compact-paced-v2-report.md). This temporally
split four-case view is diagnostic only; it does not authorize a larger run.

A subsequent new immutable same-model route trial pinned OpenRouter exclusively
to Morph with `require_parameters=true` and fallbacks disabled. The live catalog
verified slug/tag `morph`, the required strict-schema/reasoning capabilities,
and $0.08/M input, $0.28/M output, and $0.016/M cache-read rates. The first
required-wake request returned HTTP 429 without output, usage, actionable rate
headers, request ID, or provider-identifying response metadata. The trial
stopped immediately, so Morph was requested but execution was not independently
confirmed and no classification comparison was produced. Details are in
[glm-compact-morph-v3-report.md](glm-compact-morph-v3-report.md). The result does
not authorize another route or a larger run.

The final small GLM attempt used a named bounded-retry configuration in the
existing Morph runner instead of adding another near-identical runner. The
first required-wake case produced a valid, provider-confirmed Morph decision
but incorrectly stayed asleep; criteria-match and useful-intervention differed
from Jev. After 60 seconds, the second required case returned HTTP 429 without
the safe nested overload metadata required to authorize a retry, so the run
stopped after two total attempts and made no retry. Details are in
[glm-compact-morph-retry-v4-report.md](glm-compact-morph-retry-v4-report.md).
No further small GLM diagnostic loop is recommended. Continue Jev validation
against absolute gates while retaining the existing production default; resume
paired evaluation only when a stable GLM route exists.

Classifier-only runs use identical frozen windows, history size 60, wake
criteria, deterministic direct-engagement bypass, and target semantics:

```bash
# Free preflight; prints the exact maximum call count.
uv run python -m scripts.proactive_eval.benchmark_watchers \
  scripts/proactive_eval/data/benchmark-manifest.json --split tuning

# Paid tuning run. Set the ceiling to the preflight count, never higher.
uv run python -m scripts.proactive_eval.benchmark_watchers \
  scripts/proactive_eval/data/benchmark-manifest.json --split tuning \
  --confirm-paid --max-paid-calls <preflight-count> \
  --out scripts/proactive_eval/data/runs/jev-glm-tuning.json

# With Jev 1.13.0 and the classifier-only settings frozen before prediction:
uv run python -m scripts.proactive_eval.benchmark_watchers \
  scripts/proactive_eval/data/benchmark-manifest.json --split heldout \
  --confirm-paid --max-paid-calls <preflight-count> \
  --out scripts/proactive_eval/data/runs/jev-glm-heldout.json
```

Use three paired repeats per model on 120 windows per split. Repeats measure
stability; they are not independent labeled examples and do not increase the
effective label sample size. When the frozen
channel-days contain more windows, the runner makes an outcome-blind,
proportional sample by channel-day with seed `20260920`; it records the selected
case-id hash. The maximum for 240 total windows is 1,440 classifier invocations;
direct-engagement bypasses make the actual paid count lower. Tune prompts, the
TypeSafe boolean threshold, and the low-confidence threshold only on tuning. Do
not rerun held-out data to tune.

For end-to-end replay, run each held-out channel-day three times per watcher
with the same responder, skim model, tools, history size, and initial state.
Reset all agent/instruction state between runs. Use the existing command, with
only `--watcher-model` changing:

```bash
uv run python -m scripts.proactive_eval.run_guildwide <same-day-fixtures...> \
  --model gemini-3.8-flash --skim-model z-ai/glm-5.3-flash \
  --watcher-model typesafe:<pinned-version> --run-name jev-r1

uv run python -m scripts.proactive_eval.run_guildwide <same-day-fixtures...> \
  --model gemini-3.8-flash --skim-model z-ai/glm-5.3-flash \
  --watcher-model z-ai/glm-5.3-flash --run-name glm-r1
```

Score each per-channel record with the unchanged `score_run.py` judge flow.
Report classifier-only and end-to-end results separately so responder trajectory
effects are not mistaken for classifier quality.

## Budget and stop rules

The future full-benchmark planning cap is **$50**, not a spending target: $1 smoke, $10 tuning, $15
held-out classifier, and $24 end-to-end replay plus judging. Every run stops at
its explicit preflight call count and phase cap; do not increase either merely
to consume the budget. TypeSafe's public Jev price published 2026-09-15 is
$0.042 per million input tokens and $0 per million output tokens
(https://typesafe.ai/blog/introducing-system-one-models-and-jev). Those values
are frozen as the default assumptions; replace them only if the account shows
contracted rates. The GLM defaults use OpenRouter's promotional list rates
observed 2026-09-20: $0.075 input, $0.25 output, and $0.015 cache read per
million tokens (https://openrouter.ai/z-ai/glm-5.3-flash). Reconfirm both
providers' rates immediately before freezing the private manifest. The runner
refuses a full paid run without price values, an explicit call ceiling, or the
minimum frozen dataset. Stop between phases and reconcile provider dashboards
against recorded usage. Do not borrow unused budget from another phase without
updating the frozen plan before seeing held-out results.

That future plan is not authorized under the current GLM-only credential. Its
superseding ceiling is **$5 total**, including discovery, failures, and retries;
no full benchmark or end-to-end call may use it. The compact repair proposal
uses its own stricter $0.01 sub-cap and still requires explicit authorization.

End-to-end accounting uses the same public rates by default. Set
`TYPESAFE_INPUT_PRICE_PER_MILLION_USD` and
`TYPESAFE_OUTPUT_PRICE_PER_MILLION_USD` only when the account rate differs.

## Pre-declared quality criteria

The absolute Jev gates are frozen before any held-out prediction:

- required-wake recall at least 95%; with 14 held-out positives this requires
  zero misses because one miss is 92.9%;
- false-wake rate at most 5%; with 101 held-out negatives this permits at most
  five false wakes;
- no deterministic bot-directed miss;
- API/schema failure plus abstention rate at most 1% across all 120 evaluated
  windows, which permits at most one coverage failure; deterministic runtime
  bypasses are decisions but do not make provider calls.

The five null-target windows are reported separately and excluded from these
quality denominators. See [jev-review-readiness.md](jev-review-readiness.md)
for the exact candidate settings and validation limitations.

An eventual adoption decision additionally requires the held-out grouped
analysis to satisfy the comparative gates:

- paired wake precision and recall each no more than 2 percentage points below
  GLM, using a 95% bootstrap interval grouped by channel-day;
- end-to-end response precision no more than 2 points below GLM, with no
  increase in severe policy violations or bot-directed misses.

Only after those pass may cost/latency decide adoption. Prefer Jev if it reduces
classifier cost or p95 latency by at least 20% without worsening total pipeline
cost. Otherwise retain GLM or tune further. Report p50/p95 latency, tokens,
provider requests/retries, classifier cost, total pipeline cost, per-category
metrics, confidence/coverage curves, and channel-day-grouped uncertainty.

Publish aggregate results plus a few disagreements paraphrased and stripped of
names, IDs, links, and unique phrases. A production switch is a separate rollout
decision.
