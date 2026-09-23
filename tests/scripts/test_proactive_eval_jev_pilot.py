from __future__ import annotations

from collections import Counter

from scripts.proactive_eval.jev_pilot import select_negative_cases


def _case(case_id: str, channel: str, categories: list[str]) -> dict:
    return {
        "case_id": case_id,
        "expected_wake": False,
        "categories": categories,
        "meta": {"channel_id": channel},
    }


def test_negative_selection_is_distinct_deterministic_and_channel_balanced() -> None:
    cases = [
        _case(f"{channel}-{index}", channel, [category])
        for channel in ("a", "b", "c")
        for index, category in enumerate(
            ["ambient", "other_user_exchange", "useful_intervention"] * 3
        )
    ]

    selected = select_negative_cases(cases, count=14)

    assert selected == select_negative_cases(list(reversed(cases)), count=14)
    assert len({case["case_id"] for case in selected}) == 14
    assert sorted(Counter(case["meta"]["channel_id"] for case in selected).values()) == [
        4,
        5,
        5,
    ]
    assert {category for case in selected for category in case["categories"]} == {
        "ambient",
        "other_user_exchange",
        "useful_intervention",
    }
