"""Human-judged live eval for instruction-guided web summarization.

The corpus is frozen under ``tests/fixtures/web_summarizer``. Its source pages
were captured through Jina Reader, then narrowed to detailed sections so every
model receives identical content without network drift. Each output is printed
with its source hash, usage, latency, and case-specific judging rubric.

Run all available models and retain the report for review:

    uv run pytest --no-cov \
        tests/integration/test_web_summarizer_quality_eval.py -m llm -q -s

The test asserts only execution integrity. Summary quality is deliberately
human-judged against the emitted rubric rather than reduced to substring tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from pydantic_ai import Agent

from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.bot.agents.web_summarizer import SYSTEM_PROMPT
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model

try:
    import dotenv

    dotenv.load_dotenv()
except ImportError:
    pass


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "web_summarizer"
MODEL_KEYS = (
    "gemini-3-1-flash-lite",
    "poolside-laguna-s-2-1",
    "gpt-5-4-nano",
)

pytestmark = pytest.mark.llm


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    url: str
    instruction: str
    content: str
    content_sha256: str
    critical_points: tuple[str, ...]
    disallowed_claims: tuple[str, ...]


def _load_cases() -> tuple[dict[str, str], list[EvalCase]]:
    raw = yaml.safe_load((FIXTURE_DIR / "cases.yaml").read_text())
    cases: list[EvalCase] = []
    for item in raw["cases"]:
        content = (FIXTURE_DIR / item["content_file"]).read_text()
        cases.append(
            EvalCase(
                case_id=item["id"],
                title=item["title"],
                url=item["url"],
                instruction=item["instruction"],
                content=content,
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                critical_points=tuple(item["critical_points"]),
                disallowed_claims=tuple(item["disallowed_claims"]),
            )
        )
    return raw["judge_scale"], cases


JUDGE_SCALE, ALL_CASES = _load_cases()


def _selected_cases() -> list[EvalCase]:
    requested = {
        value.strip()
        for value in os.getenv("WEB_SUMMARIZER_EVAL_CASES", "").split(",")
        if value.strip()
    }
    if not requested:
        return ALL_CASES
    known = {case.case_id for case in ALL_CASES}
    unknown = requested - known
    if unknown:
        raise ValueError(f"Unknown web summarizer eval cases: {sorted(unknown)}")
    return [case for case in ALL_CASES if case.case_id in requested]


CASES = _selected_cases()


def _provider_has_key(provider: ModelProvider) -> bool:
    if provider is ModelProvider.GOOGLE:
        return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    if provider is ModelProvider.OPENROUTER:
        return bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER"))
    if provider is ModelProvider.OPENAI:
        return bool(os.getenv("OPENAI_API_KEY"))
    return False


def _summary_prompt(case: EvalCase) -> str:
    return (
        f"URL: {case.url}\n"
        f"TITLE: {case.title}\n\n"
        f"INSTRUCTION:\n{case.instruction}\n\n"
        f"CONTENT:\n{case.content}"
    )


def _eval_agent(model: CatalogModel) -> Agent[None, str]:
    return Agent(
        build_model_for(model),
        output_type=str,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings_for(model, ReasoningLevel.LOW),
    )


@pytest.mark.parametrize("model_key", MODEL_KEYS)
async def test_web_summarizer_quality(model_key: str):
    model = get_model(model_key)
    assert model is not None
    if not _provider_has_key(model.provider):
        pytest.skip(f"API key unavailable for {model.provider.value}")

    agent = _eval_agent(model)
    case_results: list[dict] = []
    for case in CASES:
        started = time.monotonic()
        result = await agent.run(_summary_prompt(case))
        elapsed = round(time.monotonic() - started, 3)
        summary = result.output.strip()
        usage = result.usage()
        assert summary, f"{model_key}/{case.case_id} returned an empty summary"
        case_results.append(
            {
                "case": case.case_id,
                "source_sha256": case.content_sha256,
                "instruction": case.instruction,
                "critical_points": case.critical_points,
                "disallowed_claims": case.disallowed_claims,
                "summary": summary,
                "latency_seconds": elapsed,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
            }
        )

    report = {
        "model_key": model.key,
        "model_id": model.model_id,
        "judge_scale": JUDGE_SCALE,
        "cases": case_results,
    }
    print("\nWEB_SUMMARIZER_EVAL=" + json.dumps(report, ensure_ascii=False))
