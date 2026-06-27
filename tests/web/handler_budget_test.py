"""Tests for the per-fire HandlerBudget — the shared metering rail."""

from __future__ import annotations

import time

import pytest

from smarter_dev.web.handler_budget import CapExceeded, HandlerBudget


def test_messages_cap_raises_on_breach():
    budget = HandlerBudget(max_messages=3)
    budget.spend_message()
    budget.spend_message()
    budget.spend_message()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_message()
    assert exc.value.cap == "messages"
    # The two-then-fail shape: three succeeded before the breach.
    assert budget.messages_sent == 3


def test_search_and_read_pools_are_independent_counters():
    budget = HandlerBudget(max_web_searches=3, max_web_reads=3)
    for _ in range(3):
        budget.spend_web_search()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_web_search()
    assert exc.value.cap == "web_searches"
    # Reads untouched by exhausting searches.
    budget.spend_web_read()
    assert budget.web_reads == 1


def test_agent_call_cap():
    budget = HandlerBudget(max_agent_calls=2)
    budget.spend_agent()
    budget.spend_agent()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_agent()
    assert exc.value.cap == "agent_calls"


def test_agent_context_byte_cap_on_input():
    budget = HandlerBudget(max_agent_context_bytes=16)
    budget.enforce_agent_context("under")  # fine
    with pytest.raises(CapExceeded) as exc:
        budget.enforce_agent_context("x" * 17)
    assert exc.value.cap == "agent_context_bytes"


def test_wall_clock_deadline():
    budget = HandlerBudget(wall_clock_seconds=0.0)
    # started_at is now; deadline already passed.
    time.sleep(0.001)
    with pytest.raises(CapExceeded) as exc:
        budget.check_deadline()
    assert exc.value.cap == "wall_clock"


def test_spend_checks_deadline_first():
    budget = HandlerBudget(max_messages=5, wall_clock_seconds=0.0)
    time.sleep(0.001)
    with pytest.raises(CapExceeded) as exc:
        budget.spend_message()
    assert exc.value.cap == "wall_clock"
    assert budget.messages_sent == 0


def test_usage_snapshot():
    budget = HandlerBudget()
    budget.spend_message()
    budget.spend_agent()
    assert budget.usage() == {
        "messages_sent": 1,
        "web_searches": 0,
        "web_reads": 0,
        "agent_calls": 1,
    }
