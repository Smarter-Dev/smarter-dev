from __future__ import annotations

from pathlib import Path

import pytest

from scripts.proactive_eval.glm_pilot import _price
from scripts.proactive_eval.glm_pilot import run
from scripts.proactive_eval.glm_pilot import summarize


def _record(expected: bool | None, wake: bool, latency: float) -> dict:
    return {
        "expected_wake": expected,
        "wake": wake,
        "failure": None,
        "latency_ms": latency,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 20,
        },
        "resolved_model_name": "z-ai/glm-5.3-flash",
    }


def test_price_uses_verified_glm_rates() -> None:
    assert _price(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
    ) == 0.265


def test_summary_keeps_ambiguous_separate() -> None:
    records = [
        _record(True, True, 10),
        _record(False, True, 20),
        _record(None, False, 30),
    ]

    result = summarize(records, 0.01)

    assert result["required_wakes"] == {
        "total": 1,
        "evaluated": 1,
        "detected": 1,
        "missed": 0,
        "errors": 0,
    }
    assert result["deterministic_negatives"] == {
        "total": 1,
        "evaluated": 1,
        "false_wakes": 1,
        "correct_no_wake": 0,
        "errors": 0,
    }
    assert result["ambiguous"] == {
        "total": 1,
        "evaluated": 1,
        "woke": 0,
        "did_not_wake": 1,
        "errors": 0,
    }
    assert result["usage"]["input_tokens"] == 300


@pytest.mark.asyncio
async def test_run_refuses_to_resume_or_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing overwrite/resume"):
        await run(
            tmp_path / "manifest.json",
            tmp_path / "selection.json",
            tmp_path / "jev.json",
            output,
        )
