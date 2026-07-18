"""Tests for the per-fire HandlerBudget — the shared metering rail."""

from __future__ import annotations

import time

import pytest

from smarter_dev.web.handler_budget import (
    ADMIN_MAX_DISCORD_READS,
    ADMIN_MAX_ROLE_CHANGES,
    ADMIN_MAX_THREAD_OPS,
    ADMIN_MAX_TIMERS,
    CapExceeded,
    DEFAULT_MAX_DISCORD_READS,
    DEFAULT_MAX_ROLE_CHANGES,
    DEFAULT_MAX_THREAD_OPS,
    DEFAULT_MAX_TIMERS,
    HandlerBudget,
    admin_budget,
)


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
        "mod_actions": 0,
        "discord_reads": 0,
        "thread_ops": 0,
        "role_changes": 0,
        "timers_scheduled": 0,
    }


def test_discord_read_cap_raises_on_breach():
    budget = HandlerBudget(max_discord_reads=2)
    budget.spend_discord_read()
    budget.spend_discord_read()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_discord_read()
    assert exc.value.cap == "discord_reads"
    assert budget.discord_reads == 2


def test_default_thread_ops_budget_is_zero_for_standard_tier():
    # Standard handlers have no mutating thread ops, like mod_actions.
    assert DEFAULT_MAX_THREAD_OPS == 0
    budget = HandlerBudget()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_thread_op()
    assert exc.value.cap == "thread_ops"
    assert budget.thread_ops == 0


def test_default_discord_reads_budget():
    assert DEFAULT_MAX_DISCORD_READS == 2
    budget = HandlerBudget()
    budget.spend_discord_read()
    budget.spend_discord_read()
    with pytest.raises(CapExceeded):
        budget.spend_discord_read()


def test_admin_budget_raises_thread_and_read_ceilings():
    assert ADMIN_MAX_DISCORD_READS == 5
    assert ADMIN_MAX_THREAD_OPS == 10
    budget = admin_budget()
    for _ in range(ADMIN_MAX_THREAD_OPS):
        budget.spend_thread_op()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_thread_op()
    assert exc.value.cap == "thread_ops"
    for _ in range(ADMIN_MAX_DISCORD_READS):
        budget.spend_discord_read()
    with pytest.raises(CapExceeded):
        budget.spend_discord_read()


def test_new_counters_appear_in_usage_after_spend():
    budget = admin_budget()
    budget.spend_discord_read()
    budget.spend_thread_op()
    usage = budget.usage()
    assert usage["discord_reads"] == 1
    assert usage["thread_ops"] == 1


def test_thread_op_checks_deadline_first():
    budget = admin_budget()
    budget.wall_clock_seconds = 0.0
    budget.started_at = time.monotonic() - 1.0
    with pytest.raises(CapExceeded) as exc:
        budget.spend_thread_op()
    assert exc.value.cap == "wall_clock"


def test_spend_role_change_increments_and_caps():
    budget = HandlerBudget(max_role_changes=2)
    budget.spend_role_change()
    budget.spend_role_change()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_role_change()
    assert exc.value.cap == "role_changes"
    assert budget.role_changes == 2


def test_default_budget_role_changes_cap_is_zero_raises_immediately():
    # Standard tier has no AdminActor, so a role change can never be spent.
    assert DEFAULT_MAX_ROLE_CHANGES == 0
    budget = HandlerBudget()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_role_change()
    assert exc.value.cap == "role_changes"
    assert budget.role_changes == 0


def test_admin_budget_sets_role_changes_cap():
    assert ADMIN_MAX_ROLE_CHANGES == 10
    budget = admin_budget()
    assert budget.max_role_changes == ADMIN_MAX_ROLE_CHANGES
    for _ in range(ADMIN_MAX_ROLE_CHANGES):
        budget.spend_role_change()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_role_change()
    assert exc.value.cap == "role_changes"


def test_usage_includes_role_changes():
    budget = admin_budget()
    budget.spend_role_change()
    assert budget.usage()["role_changes"] == 1


def test_role_change_checks_deadline_first():
    budget = admin_budget()
    budget.wall_clock_seconds = 0.0
    budget.started_at = time.monotonic() - 1.0
    with pytest.raises(CapExceeded) as exc:
        budget.spend_role_change()
    assert exc.value.cap == "wall_clock"


# -- timer arming budget (schedule_timer, E3) ----------------------------------


def test_spend_timer_default_cap():
    # Both tiers can arm timers; the default per-fire ceiling is 2.
    assert DEFAULT_MAX_TIMERS == 2
    budget = HandlerBudget()
    budget.spend_timer()
    budget.spend_timer()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_timer()
    assert exc.value.cap == "timers"
    assert budget.timers_scheduled == 2


def test_admin_budget_allows_five_timers():
    assert ADMIN_MAX_TIMERS == 5
    budget = admin_budget()
    assert budget.max_timers == ADMIN_MAX_TIMERS
    for _ in range(ADMIN_MAX_TIMERS):
        budget.spend_timer()
    with pytest.raises(CapExceeded) as exc:
        budget.spend_timer()
    assert exc.value.cap == "timers"


def test_usage_includes_timers_scheduled():
    budget = HandlerBudget()
    assert budget.usage()["timers_scheduled"] == 0
    budget.spend_timer()
    assert budget.usage()["timers_scheduled"] == 1


def test_timer_checks_deadline_first():
    budget = HandlerBudget()
    budget.wall_clock_seconds = 0.0
    budget.started_at = time.monotonic() - 1.0
    with pytest.raises(CapExceeded) as exc:
        budget.spend_timer()
    assert exc.value.cap == "wall_clock"
