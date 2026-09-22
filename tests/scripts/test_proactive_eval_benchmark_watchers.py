from __future__ import annotations

from scripts.proactive_eval.benchmark_watchers import _cost
from scripts.proactive_eval.benchmark_watchers import grouped_bootstrap
from scripts.proactive_eval.benchmark_watchers import repeat_stability
from scripts.proactive_eval.benchmark_watchers import sample_cases
from scripts.proactive_eval.benchmark_watchers import score


def _result(model: str, expected: bool | None, wake: bool, latency: float) -> dict:
    return {
        "model_id": model,
        "expected_wake": expected,
        "wake": wake,
        "latency_ms": latency,
        "abstained": False,
        "failure": None,
        "usage_by_model": {
            model: {
                "input_tokens": 10,
                "output_tokens": 1,
                "cache_read_tokens": 0,
            }
        },
        "provider_requests": 1,
        "resolved_model_name": "resolved" if model.startswith("typesafe:") else None,
        "categories": ["useful_intervention"],
        "fixture": "day-1.jsonl",
        "case_id": f"day-1::{model}:{expected}:{latency}",
        "repeat": 0,
    }


def test_score_reports_paired_quality_coverage_latency_and_requests() -> None:
    records = [
        _result("typesafe:jev-latest", True, True, 10),
        _result("typesafe:jev-latest", False, True, 30),
        _result("typesafe:jev-latest", None, False, 20),
        _result("z-ai/glm-5.3-flash", True, False, 40),
        _result("z-ai/glm-5.3-flash", False, False, 60),
    ]

    result = score(records)

    jev = result["typesafe:jev-latest"]
    assert jev["wake_precision"] == 0.5
    assert jev["wake_recall"] == 1.0
    assert jev["false_wake_rate"] == 1.0
    assert jev["ambiguous_samples"] == 1
    assert jev["latency_ms"]["p50"] == 20
    assert jev["provider_requests"] == 3
    assert jev["resolved_model_names"] == ["resolved"]
    assert jev["usage_by_model"]["typesafe:jev-latest"]["input_tokens"] == 30
    assert jev["per_category"]["useful_intervention"]["wake_recall"] == 1.0

    glm = result["z-ai/glm-5.3-flash"]
    assert glm["wake_recall"] == 0.0
    assert glm["false_wake_rate"] == 0.0


def test_grouped_bootstrap_reports_paired_intervals() -> None:
    records = [
        _result("typesafe:jev-latest", True, True, 10),
        _result("typesafe:jev-latest", False, False, 10),
        _result("z-ai/glm-5.3-flash", True, False, 10),
        _result("z-ai/glm-5.3-flash", False, False, 10),
    ]

    result = grouped_bootstrap(records, iterations=20, seed=1)

    assert result["iterations"] == 20
    assert result["intervals"]["wake_recall"] == {"lower": 1.0, "upper": 1.0}


def test_sample_cases_is_deterministic_proportional_and_outcome_blind() -> None:
    cases = [
        {
            "case_id": f"day-a::{index}",
            "fixture": "day-a.jsonl",
            "expected_wake": index % 2 == 0,
        }
        for index in range(8)
    ] + [
        {
            "case_id": f"day-b::{index}",
            "fixture": "day-b.jsonl",
            "expected_wake": index % 2 == 0,
        }
        for index in range(4)
    ]

    selected = sample_cases(cases, limit=6, seed=7)

    assert selected == sample_cases(cases, limit=6, seed=7)
    assert sum(case["fixture"] == "day-a.jsonl" for case in selected) == 4
    assert sum(case["fixture"] == "day-b.jsonl" for case in selected) == 2


def test_glm_cost_treats_cached_tokens_as_subset_of_input() -> None:
    prices = {
        "glm_input": 0.075,
        "glm_output": 0.25,
        "glm_cache_read": 0.015,
    }

    cost = _cost(
        "z-ai/glm-5.3-flash",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
        },
        prices,
    )

    assert cost == 0.265


def test_score_excludes_failures_from_classification_counts() -> None:
    valid = _result("typesafe:jev-1.13.0", True, True, 10)
    failed = _result("typesafe:jev-1.13.0", True, False, 20)
    failed["failure"] = "TimeoutError"

    result = score([valid, failed])["typesafe:jev-1.13.0"]

    assert result["labeled_samples"] == 2
    assert result["eligible_samples"] == 1
    assert result["unevaluable_labeled_samples"] == 1
    assert result["false_negative"] == 0
    assert result["failures"] == 1


def test_repeat_stability_does_not_count_repeats_as_new_cases() -> None:
    records = []
    for repeat, wake in enumerate((True, True, True)):
        record = _result("typesafe:jev-1.13.0", True, wake, 10)
        record.update({"case_id": "day-1::1", "repeat": repeat})
        records.append(record)
    for repeat, wake in enumerate((False, True, False)):
        record = _result("typesafe:jev-1.13.0", False, wake, 10)
        record.update({"case_id": "day-1::2", "repeat": repeat})
        records.append(record)

    result = repeat_stability(records, expected_repeats=3)

    assert result["distinct_cases"] == 2
    assert result["stable_wake_cases"] == 1
    assert result["variable_wake_cases"] == 1
