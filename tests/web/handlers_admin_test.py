"""Tests for the admin handlers controller's error-log grouping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from smarter_dev.web.handlers_admin import _group_error_runs


@dataclass
class _Run:
    handler_id: str
    fired_at: datetime
    outcome: str
    cap: str | None = None
    error: str | None = None


def _at(minute: int) -> datetime:
    return datetime(2026, 6, 28, 12, minute, tzinfo=timezone.utc)


def test_groups_by_handler_id_as_string():
    rows = [
        _Run("h1", _at(3), "error", error="boom"),
        _Run("h2", _at(2), "cap_exceeded", cap="messages"),
        _Run("h1", _at(1), "cap_exceeded", cap="memory_size"),
    ]
    grouped = _group_error_runs(rows)
    assert set(grouped) == {"h1", "h2"}
    assert len(grouped["h1"]) == 2
    assert grouped["h2"][0]["cap"] == "messages"
    # Each entry exposes exactly the display fields.
    assert grouped["h1"][0] == {
        "fired_at": _at(3),
        "outcome": "error",
        "cap": None,
        "error": "boom",
    }


def test_preserves_input_order_newest_first():
    rows = [
        _Run("h1", _at(9), "error", error="newest"),
        _Run("h1", _at(5), "error", error="middle"),
        _Run("h1", _at(1), "error", error="oldest"),
    ]
    grouped = _group_error_runs(rows)
    assert [e["error"] for e in grouped["h1"]] == ["newest", "middle", "oldest"]


def test_truncates_to_per_handler_cap_keeping_first_seen():
    rows = [_Run("h1", _at(m), "error", error=str(m)) for m in range(10, 0, -1)]
    grouped = _group_error_runs(rows, per_handler=3)
    # Rows arrive newest-first; keep the first 3 (the most recent).
    assert [e["error"] for e in grouped["h1"]] == ["10", "9", "8"]


def test_empty_rows_give_empty_mapping():
    assert _group_error_runs([]) == {}
