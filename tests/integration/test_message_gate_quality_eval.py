"""Offline-scored live eval for the Discord message gate.

Compares gate arms on the frozen, labeled cases in
``tests/fixtures/message_gate/cases.yaml``:

- ``jev`` (production): the gate's own ``message_gate.judge_messages`` — one
  bool question per candidate, one request per call — through
  ``message_gate_jev.py``.
- any catalog key (e.g. ``gpt-5-4-nano``, the gate before 2026-09-24): an Agent
  built as that gate was (``message_gate_llm.SYSTEM_PROMPT``,
  ``output_type=GateDecision``, model settings at ``ReasoningLevel.NONE``) on
  the prompt from ``message_gate_llm.render_prompt``.

Each arm is scored per case: exact match (after removing ``either`` ids from
both sides), false allows, false drops, context-id leaks and hallucinated ids.
The summary table adds tokens, cost (``llm_pricing.calc_cost``; "n/a" for Jev,
which has no price entry) and p50/p95 latency; a per-case diff lists every case
where an arm disagrees with the label or the arms disagree with each other.

Run (arms whose provider key is missing are skipped):

    uv run --all-groups pytest --no-cov \\
        tests/integration/test_message_gate_quality_eval.py -m llm -q -s

Environment:
    MESSAGE_GATE_EVAL_MODELS   comma-separated arms (default: jev)
    MESSAGE_GATE_EVAL_CASES    comma-separated case ids to run (default: all)
    MESSAGE_GATE_EVAL_REPORT=1 also write reports/message_gate_eval_<timestamp>.json
    MESSAGE_GATE_JEV_MODEL     Jev model id (default typesafe:jev-1.13.0)
    MESSAGE_GATE_JEV_BOOLEAN_THRESHOLD  typesafe_boolean_threshold (default 0.5)

The test asserts only execution integrity: every case got a parseable decision
from every arm that ran. Quality is read from the printed report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from pydantic_ai import Agent

from smarter_dev.bot.agents.message_gate import GateMessage
from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model
from tests.integration.message_gate_jev import JEV_LOW_CONFIDENCE
from tests.integration.message_gate_jev import JevGate
from tests.integration.message_gate_jev import build_jev_model
from tests.integration.message_gate_jev import jev_boolean_threshold
from tests.integration.message_gate_jev import jev_model_id
from tests.integration.message_gate_llm import SYSTEM_PROMPT
from tests.integration.message_gate_llm import GateDecision
from tests.integration.message_gate_llm import render_prompt

CASES_PATH = Path(__file__).parents[1] / "fixtures" / "message_gate" / "cases.yaml"
REPORTS_DIR = Path(__file__).parents[2] / "reports"
JEV_ARM = "jev"
MODEL_KEYS = (JEV_ARM,)

pytestmark = pytest.mark.llm


# --------------------------------------------------------------------- cases


@dataclass(frozen=True)
class GateCase:
    case_id: str
    category: str
    instructions: str
    channel_name: str | None
    grounding: tuple[GateMessage, ...]
    candidates: tuple[GateMessage, ...]
    expected_allowed: frozenset[str]
    either: frozenset[str]
    notes: str = ""


def _messages(raw: list[dict]) -> tuple[GateMessage, ...]:
    return tuple(
        GateMessage(
            message_id=str(item["id"]),
            author_display=item["author"],
            content=item["content"],
        )
        for item in raw or []
    )


def load_cases(path: Path = CASES_PATH) -> list[GateCase]:
    raw = yaml.safe_load(path.read_text())
    return [
        GateCase(
            case_id=item["id"],
            category=item.get("category", "uncategorized"),
            instructions=item["instructions"],
            channel_name=item.get("channel_name"),
            grounding=_messages(item.get("grounding")),
            candidates=_messages(item["candidates"]),
            expected_allowed=frozenset(
                str(i) for i in item.get("expected_allowed") or []
            ),
            either=frozenset(str(i) for i in item.get("either") or []),
            notes=item.get("notes", ""),
        )
        for item in raw["cases"]
    ]


def _selected_cases(cases: list[GateCase]) -> list[GateCase]:
    requested = {
        value.strip()
        for value in os.getenv("MESSAGE_GATE_EVAL_CASES", "").split(",")
        if value.strip()
    }
    if not requested:
        return cases
    unknown = requested - {case.case_id for case in cases}
    if unknown:
        raise ValueError(f"Unknown message gate eval cases: {sorted(unknown)}")
    return [case for case in cases if case.case_id in requested]


def _selected_arms() -> tuple[str, ...]:
    raw = os.getenv("MESSAGE_GATE_EVAL_MODELS", "")
    keys = tuple(value.strip() for value in raw.split(",") if value.strip())
    return keys or MODEL_KEYS


# ------------------------------------------------------------------- scoring


@dataclass
class CaseScore:
    """One arm's decision on one case, scored against the label.

    ``returned`` is what production would act on: the raw ids intersected with
    the candidate ids. ``either`` ids are removed from both sides before the
    exact-match / false-allow / false-drop comparison.
    """

    case_id: str
    raw_ids: list[str]
    returned: list[str]
    exact: bool
    false_allows: list[str]
    false_drops: list[str]
    leaks: list[str]
    hallucinated: list[str]
    candidates_scored: int
    candidates_correct: int


def score_case(case: GateCase, raw_ids: list[str]) -> CaseScore:
    candidate_ids = [m.message_id for m in case.candidates]
    candidate_set = set(candidate_ids)
    grounding_set = {m.message_id for m in case.grounding}
    raw = [str(i) for i in raw_ids]
    returned_set = set(raw) & candidate_set
    returned = [i for i in candidate_ids if i in returned_set]

    scored = [i for i in candidate_ids if i not in case.either]
    expected = set(case.expected_allowed) - case.either
    got = returned_set - case.either
    false_allows = [i for i in scored if i in got and i not in expected]
    false_drops = [i for i in scored if i in expected and i not in got]
    leaks = sorted({i for i in raw if i in grounding_set})
    hallucinated = sorted(
        {i for i in raw if i not in candidate_set and i not in grounding_set}
    )
    return CaseScore(
        case_id=case.case_id,
        raw_ids=raw,
        returned=returned,
        exact=not false_allows and not false_drops,
        false_allows=false_allows,
        false_drops=false_drops,
        leaks=leaks,
        hallucinated=hallucinated,
        candidates_scored=len(scored),
        candidates_correct=len(scored) - len(false_allows) - len(false_drops),
    )


@dataclass
class ArmCaseResult:
    score: CaseScore
    latency_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 1
    error: str | None = None
    # candidate id -> classifier confidence (Jev only)
    confidence: dict[str, float] = field(default_factory=dict)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil without math
    return ordered[int(rank) - 1]


@dataclass
class ArmSummary:
    arm: str
    model_id: str
    cases: int
    exact: int
    accuracy: float
    candidates_scored: int
    candidates_correct: int
    candidate_accuracy: float
    false_allows: int
    false_drops: int
    leaks: int
    hallucinated: int
    errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    requests: int
    cost_usd: str
    p50_latency: float | None
    p95_latency: float | None
    low_confidence: int | None = None
    low_confidence_wrong: int | None = None
    judgments_with_confidence: int | None = None


def summarize_arm(
    arm: str,
    model_id: str,
    results: list[ArmCaseResult],
    cost: Callable[[int, int, int], object] | None,
    low_confidence_threshold: float | None = None,
) -> ArmSummary:
    """Aggregate one arm. ``cost(input, output, cache_read)`` or None for n/a."""
    input_tokens = sum(r.input_tokens for r in results)
    output_tokens = sum(r.output_tokens for r in results)
    cache_read = sum(r.cache_read_tokens for r in results)
    scored = sum(r.score.candidates_scored for r in results)
    correct = sum(r.score.candidates_correct for r in results)
    exact = sum(r.score.exact for r in results)
    latencies = [r.latency_seconds for r in results]
    summary = ArmSummary(
        arm=arm,
        model_id=model_id,
        cases=len(results),
        exact=exact,
        accuracy=exact / len(results) if results else 0.0,
        candidates_scored=scored,
        candidates_correct=correct,
        candidate_accuracy=correct / scored if scored else 0.0,
        false_allows=sum(len(r.score.false_allows) for r in results),
        false_drops=sum(len(r.score.false_drops) for r in results),
        leaks=sum(len(r.score.leaks) for r in results),
        hallucinated=sum(len(r.score.hallucinated) for r in results),
        errors=sum(r.error is not None for r in results),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        requests=sum(r.requests for r in results),
        cost_usd="n/a"
        if cost is None
        else f"{cost(input_tokens, output_tokens, cache_read):.6f}",
        p50_latency=percentile(latencies, 50),
        p95_latency=percentile(latencies, 95),
    )
    if low_confidence_threshold is not None:
        judged = 0
        low = 0
        low_wrong = 0
        for r in results:
            wrong = set(r.score.false_allows) | set(r.score.false_drops)
            for message_id, value in r.confidence.items():
                judged += 1
                if value < low_confidence_threshold:
                    low += 1
                    low_wrong += message_id in wrong
        summary.judgments_with_confidence = judged
        summary.low_confidence = low
        summary.low_confidence_wrong = low_wrong
    return summary


def _fmt_latency(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def format_summary_table(summaries: list[ArmSummary]) -> str:
    header = (
        f"{'arm':<16} {'exact':>9} {'cand acc':>9} {'F-allow':>7} {'F-drop':>6} "
        f"{'leaks':>5} {'halluc':>6} {'errors':>6} {'in tok':>8} {'out tok':>8} "
        f"{'cost $':>10} {'p50':>7} {'p95':>7} {'low conf':>10}"
    )
    lines = [header, "-" * len(header)]
    for s in summaries:
        low = "-"
        if s.low_confidence is not None:
            low = f"{s.low_confidence}/{s.judgments_with_confidence}"
        lines.append(
            f"{s.arm:<16} {f'{s.exact}/{s.cases}':>9} {s.candidate_accuracy:>8.1%} "
            f"{s.false_allows:>7} {s.false_drops:>6} {s.leaks:>5} {s.hallucinated:>6} "
            f"{s.errors:>6} {s.input_tokens:>8} {s.output_tokens:>8} {s.cost_usd:>10} "
            f"{_fmt_latency(s.p50_latency):>7} {_fmt_latency(s.p95_latency):>7} {low:>10}"
        )
    return "\n".join(lines)


def _label(case: GateCase, message_id: str) -> str:
    if message_id in case.either:
        return "either"
    return "ALLOW" if message_id in case.expected_allowed else "drop"


def _verdict(result: ArmCaseResult, message_id: str) -> str:
    mark = "ALLOW" if message_id in result.score.returned else "drop"
    if message_id in result.confidence:
        mark += f"({result.confidence[message_id]:.2f})"
    return mark


def format_case_diff(
    cases: list[GateCase], results: dict[str, dict[str, ArmCaseResult]]
) -> str:
    """Cases where an arm misses the label or arms disagree on a scored candidate.

    ``results`` is ``arm -> case_id -> result``. ``either`` candidates are shown
    but never flag a case on their own.
    """
    arms = list(results)
    blocks: list[str] = []
    for case in cases:
        per_arm = {
            arm: results[arm][case.case_id]
            for arm in arms
            if case.case_id in results[arm]
        }
        if not per_arm:
            continue
        missed = [arm for arm, r in per_arm.items() if not r.score.exact]
        scored_ids = [
            m.message_id for m in case.candidates if m.message_id not in case.either
        ]
        verdict_sets = {
            tuple(i in r.score.returned for i in scored_ids) for r in per_arm.values()
        }
        extras = [
            arm
            for arm, r in per_arm.items()
            if r.score.leaks or r.score.hallucinated or r.error
        ]
        if not missed and len(verdict_sets) <= 1 and not extras:
            continue
        tags = []
        if missed:
            tags.append("wrong: " + ", ".join(missed))
        if len(verdict_sets) > 1:
            tags.append("arms disagree")
        lines = [f"{case.case_id} [{case.category}] " + "; ".join(tags)]
        for message in case.candidates:
            snippet = " ".join(message.content.split())
            if len(snippet) > 70:
                snippet = snippet[:67] + "..."
            verdicts = "  ".join(
                f"{arm}={_verdict(r, message.message_id)}" for arm, r in per_arm.items()
            )
            lines.append(
                f"    {message.author_display}: {snippet!r}\n"
                f"      expected={_label(case, message.message_id)}  {verdicts}"
            )
        for arm, r in per_arm.items():
            if r.score.leaks:
                lines.append(f"    {arm} leaked context ids {r.score.leaks}")
            if r.score.hallucinated:
                lines.append(f"    {arm} hallucinated ids {r.score.hallucinated}")
            if r.error:
                lines.append(f"    {arm} error (failed open): {r.error}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(no disagreements)"


# ---------------------------------------------------------------------- arms


CaseRunner = Callable[[GateCase], Awaitable[ArmCaseResult]]


@dataclass
class Arm:
    key: str
    model_id: str
    run: CaseRunner
    cost: Callable[[int, int, int], object] | None
    low_confidence_threshold: float | None = None
    settings_note: str = ""


def _provider_has_key(provider: ModelProvider) -> bool:
    if provider is ModelProvider.GOOGLE:
        return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    if provider is ModelProvider.OPENROUTER:
        return bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER"))
    if provider is ModelProvider.OPENAI:
        return bool(os.getenv("OPENAI_API_KEY"))
    return False


def _jev_has_key() -> bool:
    return bool(os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_API_KEY"))


def gate_agent_for(model: CatalogModel) -> Agent[None, GateDecision]:
    """The generative gate agent for ``model``, built as the pre-Jev gate was."""
    return Agent(
        build_model_for(model),
        output_type=GateDecision,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings_for(model, ReasoningLevel.NONE),
    )


def llm_case_runner(agent: Agent[None, GateDecision]) -> CaseRunner:
    async def run(case: GateCase) -> ArmCaseResult:
        prompt = render_prompt(
            case.instructions,
            list(case.candidates),
            list(case.grounding),
            case.channel_name,
        )
        started = time.monotonic()
        try:
            result = await agent.run(prompt)
        except Exception as error:  # noqa: BLE001 - score the fail-open verdict
            elapsed = round(time.monotonic() - started, 3)
            all_ids = [m.message_id for m in case.candidates]
            return ArmCaseResult(
                score=score_case(case, all_ids),
                latency_seconds=elapsed,
                error=f"{type(error).__name__}: {error}"[:300],
            )
        elapsed = round(time.monotonic() - started, 3)
        usage = result.usage
        return ArmCaseResult(
            score=score_case(case, list(result.output.allowed_message_ids)),
            latency_seconds=elapsed,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_tokens or 0,
            requests=usage.requests or 1,
        )

    return run


def jev_case_runner(gate: JevGate) -> CaseRunner:
    async def run(case: GateCase) -> ArmCaseResult:
        decision = await gate.decide(
            case.instructions,
            list(case.candidates),
            list(case.grounding),
            case.channel_name,
        )
        return ArmCaseResult(
            score=score_case(case, decision.allowed_ids),
            latency_seconds=decision.latency_seconds,
            input_tokens=decision.input_tokens,
            output_tokens=decision.output_tokens,
            requests=decision.requests,
            error="; ".join(decision.errors) or None,
            confidence=dict(decision.confidence),
        )

    return run


def _build_arm(key: str) -> Arm | str:
    """The arm for ``key``, or a reason string when it has to be skipped."""
    if key == JEV_ARM:
        if not _jev_has_key():
            return "TYPESAFE_API_KEY / JEV_API_KEY unavailable"
        threshold = jev_boolean_threshold()
        model_id = jev_model_id()
        return Arm(
            key=key,
            model_id=model_id,
            run=jev_case_runner(
                JevGate(build_jev_model(model_id), boolean_threshold=threshold)
            ),
            cost=None,  # no Jev price in smarter_dev/web/llm_pricing.py
            low_confidence_threshold=JEV_LOW_CONFIDENCE,
            settings_note=f"typesafe_boolean_threshold={threshold}",
        )
    model = get_model(key)
    if model is None:
        raise ValueError(f"Unknown catalog model key {key!r}")
    if not _provider_has_key(model.provider):
        return f"API key unavailable for {model.provider.value}"
    from smarter_dev.web.llm_pricing import calc_cost

    return Arm(
        key=key,
        model_id=model.model_id,
        run=llm_case_runner(gate_agent_for(model)),
        cost=lambda i, o, c, _id=model.model_id: calc_cost(
            i, o, _id, cache_read_tokens=c
        ),
        settings_note=f"reasoning={ReasoningLevel.NONE.value}",
    )


async def run_arm(arm: Arm, cases: list[GateCase]) -> dict[str, ArmCaseResult]:
    """Run every case through ``arm`` sequentially, so latency is per call."""
    return {case.case_id: await arm.run(case) for case in cases}


# ---------------------------------------------------------------------- test


async def test_message_gate_quality():
    try:
        import dotenv

        dotenv.load_dotenv()
    except ImportError:
        pass

    cases = _selected_cases(load_cases())
    arms: list[Arm] = []
    skipped: dict[str, str] = {}
    for key in _selected_arms():
        built = _build_arm(key)
        if isinstance(built, str):
            skipped[key] = built
        else:
            arms.append(built)
    if not arms:
        pytest.skip(
            "No gate arm has a provider key: "
            + "; ".join(f"{k}: {v}" for k, v in skipped.items())
        )

    # Arms hit different providers, so they run side by side; cases within an
    # arm run one at a time to keep latency figures per call.
    outcomes = await asyncio.gather(*(run_arm(arm, cases) for arm in arms))
    results = {arm.key: outcome for arm, outcome in zip(arms, outcomes)}
    summaries = [
        summarize_arm(
            arm.key,
            arm.model_id,
            list(results[arm.key].values()),
            arm.cost,
            arm.low_confidence_threshold,
        )
        for arm in arms
    ]

    print(
        f"\nMESSAGE GATE EVAL: {len(cases)} cases, {sum(len(c.candidates) for c in cases)} candidates"
    )
    for arm in arms:
        print(f"  {arm.key}: {arm.model_id} ({arm.settings_note})")
    for key, reason in skipped.items():
        print(f"  {key}: SKIPPED ({reason})")
    print()
    print(format_summary_table(summaries))
    if any(arm.low_confidence_threshold is not None for arm in arms):
        print(
            f"low conf = judgments with confidence < {JEV_LOW_CONFIDENCE} / judgments; "
            + ", ".join(
                f"{s.arm}: {s.low_confidence_wrong} of the low ones were wrong"
                for s in summaries
                if s.low_confidence is not None
            )
        )
    print("\nPER-CASE DIFF (label vs arms, verdicts on each candidate):\n")
    print(format_case_diff(cases, results))

    if os.getenv("MESSAGE_GATE_EVAL_REPORT") == "1":
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        REPORTS_DIR.mkdir(exist_ok=True)
        path = REPORTS_DIR / f"message_gate_eval_{stamp}.json"
        report = {
            "generated_at": stamp,
            "cases_file": "tests/fixtures/message_gate/cases.yaml",
            "cases_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
            "arms": {
                arm.key: {"model_id": arm.model_id, "settings": arm.settings_note}
                for arm in arms
            },
            "skipped": skipped,
            "summary": [asdict(s) for s in summaries],
            "cases": [
                {
                    "id": case.case_id,
                    "category": case.category,
                    "expected_allowed": sorted(case.expected_allowed),
                    "either": sorted(case.either),
                    "arms": {
                        arm.key: {
                            **asdict(results[arm.key][case.case_id].score),
                            "latency_seconds": results[arm.key][
                                case.case_id
                            ].latency_seconds,
                            "input_tokens": results[arm.key][case.case_id].input_tokens,
                            "output_tokens": results[arm.key][
                                case.case_id
                            ].output_tokens,
                            "error": results[arm.key][case.case_id].error,
                            "confidence": results[arm.key][case.case_id].confidence,
                        }
                        for arm in arms
                    },
                }
                for case in cases
            ],
        }
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nreport written to {path}")

    # Execution integrity only: every arm produced a decision for every case
    # without failing open.
    failures = [
        f"{arm}/{case_id}: {r.error}"
        for arm, per_case in results.items()
        for case_id, r in per_case.items()
        if r.error
    ]
    assert all(len(per_case) == len(cases) for per_case in results.values())
    assert not failures, "Gate calls failed (scored as fail-open):\n" + "\n".join(
        failures
    )
