"""Ground-truth schema for proactive watcher decisions.

The older ``*.labels.json`` files answer whether a response would be allowed.
This module deliberately keeps the stronger "must the watcher wake?" judgment
in a separate, human-adjudicated sidecar.
"""

from __future__ import annotations

from collections.abc import Iterable

from scripts.proactive_eval.labels import LABELABLE_MESSAGE_TYPES

POLICY_VERSION = "proactive-wake-v1"
WAKE_CATEGORIES = {
    "direct_engagement",
    "watch_instruction",
    "useful_intervention",
    "follow_up",
    "other_user_exchange",
    "ambient",
}


def labelable_ids(records: Iterable[dict]) -> set[str]:
    return {
        record["id"]
        for record in records
        if not record["is_bot"]
        and record["message_type"] in LABELABLE_MESSAGE_TYPES
    }


def validate_document(records: list[dict], document: dict) -> dict[str, int]:
    """Validate complete, output-independent wake labels for one fixture."""
    if document.get("policy_version") != POLICY_VERSION:
        raise ValueError(
            f"wake labels must use policy_version {POLICY_VERSION!r}"
        )
    labels = document.get("labels")
    if not isinstance(labels, dict):
        raise ValueError("wake labels must contain an object named 'labels'")
    expected = labelable_ids(records)
    got = set(labels)
    if missing := expected - got:
        raise ValueError(f"wake labels missing message ids: {sorted(missing)}")
    if extra := got - expected:
        raise ValueError(f"wake labels contain extra message ids: {sorted(extra)}")

    counts = {"required": 0, "not_required": 0, "ambiguous": 0}
    for message_id, label in labels.items():
        required = label.get("required_wake")
        if required not in (True, False, None):
            raise ValueError(
                f"id {message_id}: required_wake must be true, false, or null"
            )
        category = label.get("category")
        if category not in WAKE_CATEGORIES:
            raise ValueError(
                f"id {message_id}: category must be one of "
                f"{sorted(WAKE_CATEGORIES)}"
            )
        if not str(label.get("reason", "")).strip():
            raise ValueError(f"id {message_id}: reason must not be empty")
        bucket = (
            "required"
            if required is True
            else "not_required"
            if required is False
            else "ambiguous"
        )
        counts[bucket] += 1
    return counts


def window_ground_truth(
    new_records: Iterable[dict], labels: dict[str, dict]
) -> tuple[bool | None, list[str]]:
    """Collapse message labels to the exact burst-level target the watcher sees."""
    window_labels = [
        labels[record["id"]]
        for record in new_records
        if record["id"] in labels
    ]
    if not window_labels:
        return None, []
    categories = sorted({label["category"] for label in window_labels})
    if any(label["required_wake"] is True for label in window_labels):
        return True, categories
    if any(label["required_wake"] is None for label in window_labels):
        return None, categories
    return False, categories
