"""Offline checks for the message-gate eval: fixture schema and scorer.

The live eval (``tests/integration/test_message_gate_quality_eval.py``) is
``llm``-marked and skipped by default, so a malformed fixture or a scoring bug
would otherwise only surface when someone pays for a run.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

from tests.integration.test_message_gate_quality_eval import load_cases
from tests.integration.test_message_gate_quality_eval import percentile
from tests.integration.test_message_gate_quality_eval import score_case

CASES_PATH = Path(__file__).parents[2] / "fixtures" / "message_gate" / "cases.yaml"
CASE_KEYS = {
    "id",
    "category",
    "instructions",
    "channel_name",
    "grounding",
    "candidates",
    "expected_allowed",
    "either",
    "notes",
}
MESSAGE_KEYS = {"id", "author", "content"}


def _raw_cases() -> list[dict]:
    return yaml.safe_load(CASES_PATH.read_text())["cases"]


def test_case_ids_are_unique():
    counts = Counter(case["id"] for case in _raw_cases())
    assert [case_id for case_id, n in counts.items() if n > 1] == []


def test_cases_have_the_expected_shape():
    for case in _raw_cases():
        assert set(case) <= CASE_KEYS, (case["id"], set(case) - CASE_KEYS)
        assert isinstance(case["instructions"], str) and case["instructions"].strip(), (
            case["id"]
        )
        assert case["candidates"], case["id"]
        assert isinstance(case["expected_allowed"], list), case["id"]
        for message in case["candidates"] + (case.get("grounding") or []):
            assert set(message) == MESSAGE_KEYS, (case["id"], message)
            # Ids must stay strings: an unquoted snowflake would load as int.
            assert isinstance(message["id"], str), (case["id"], message["id"])
            assert isinstance(message["content"], str) and message["content"], case[
                "id"
            ]


def test_label_ids_are_candidates_and_never_grounding():
    for case in _raw_cases():
        candidate_ids = [m["id"] for m in case["candidates"]]
        grounding_ids = [m["id"] for m in case.get("grounding") or []]
        assert len(set(candidate_ids)) == len(candidate_ids), case["id"]
        assert len(set(grounding_ids)) == len(grounding_ids), case["id"]
        assert set(case["expected_allowed"]) <= set(candidate_ids), case["id"]
        assert set(case.get("either") or []) <= set(candidate_ids), case["id"]
        assert not set(candidate_ids) & set(grounding_ids), case["id"]


def test_message_ids_are_unique_across_the_whole_fixture():
    ids = Counter(
        m["id"]
        for case in _raw_cases()
        for m in case["candidates"] + (case.get("grounding") or [])
    )
    assert [i for i, n in ids.items() if n > 1] == []


def test_loader_reads_every_case():
    cases = load_cases(CASES_PATH)
    assert len(cases) == len(_raw_cases())
    assert all(case.candidates for case in cases)


# ------------------------------------------------------------------- scorer


def _case(case_id: str):
    return next(case for case in load_cases(CASES_PATH) if case.case_id == case_id)


def test_score_exact_match_ignores_either_ids_and_order():
    case = _case("short_messages_in_on_topic_thread")
    thanks, lol, followup = (m.message_id for m in case.candidates)

    assert score_case(case, [followup]).exact
    # Allowing the either-ids is equally correct.
    both = score_case(case, [lol, followup, thanks])
    assert both.exact
    assert both.candidates_scored == 1 and both.candidates_correct == 1
    assert both.returned == [thanks, lol, followup]


def test_score_counts_false_allows_and_false_drops():
    case = _case("batch_python_mixed")
    ids = [m.message_id for m in case.candidates]
    # Expected: ids[0] and ids[3]. Allow ids[0], ids[1]; drop ids[3].
    score = score_case(case, [ids[1], ids[0]])
    assert not score.exact
    assert score.false_allows == [ids[1]]
    assert score.false_drops == [ids[3]]
    assert score.candidates_scored == 5 and score.candidates_correct == 3


def test_score_reports_leaks_and_hallucinations_without_acting_on_them():
    case = _case("injection_requests_context_id")
    grounding_id = case.grounding[0].message_id
    good = case.candidates[1].message_id
    score = score_case(case, [good, grounding_id, "999"])
    # Production intersects with the candidates, so only `good` is acted on.
    assert score.returned == [good]
    assert score.exact
    assert score.leaks == [grounding_id]
    assert score.hallucinated == ["999"]


def test_score_empty_expected_and_empty_answer():
    case = _case("python_plain_off_topic")
    assert score_case(case, []).exact
    assert score_case(case, [case.candidates[0].message_id]).false_allows == [
        case.candidates[0].message_id
    ]


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([3.0], 95) == 3.0
    values = [float(v) for v in range(1, 21)]  # 1..20
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0
