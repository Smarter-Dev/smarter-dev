from __future__ import annotations

import pytest

from scripts.proactive_eval import wake_labels


def _record(message_id: str, *, bot: bool = False, message_type: int = 0) -> dict:
    return {
        "id": message_id,
        "is_bot": bot,
        "message_type": message_type,
    }


def _label(required: bool | None, category: str = "ambient") -> dict:
    return {
        "required_wake": required,
        "category": category,
        "reason": "independent adjudication",
    }


def test_validate_requires_complete_human_message_coverage() -> None:
    records = [_record("1"), _record("2", bot=True), _record("3", message_type=6)]
    document = {
        "policy_version": wake_labels.POLICY_VERSION,
        "labels": {"1": _label(False)},
    }

    assert wake_labels.validate_document(records, document) == {
        "required": 0,
        "not_required": 1,
        "ambiguous": 0,
    }


def test_validate_rejects_missing_and_extra_ids() -> None:
    records = [_record("1")]
    base = {"policy_version": wake_labels.POLICY_VERSION, "labels": {}}
    with pytest.raises(ValueError, match="missing"):
        wake_labels.validate_document(records, base)

    base["labels"] = {"1": _label(False), "2": _label(False)}
    with pytest.raises(ValueError, match="extra"):
        wake_labels.validate_document(records, base)


def test_window_ground_truth_prefers_required_then_ambiguous() -> None:
    records = [_record("1"), _record("2")]
    labels = {
        "1": _label(None, "follow_up"),
        "2": _label(True, "direct_engagement"),
    }
    assert wake_labels.window_ground_truth(records, labels) == (
        True,
        ["direct_engagement", "follow_up"],
    )

    labels["2"] = _label(False, "ambient")
    assert wake_labels.window_ground_truth(records, labels)[0] is None


def test_window_ground_truth_false_when_every_message_is_negative() -> None:
    records = [_record("1"), _record("2")]
    labels = {"1": _label(False), "2": _label(False, "other_user_exchange")}

    assert wake_labels.window_ground_truth(records, labels)[0] is False
