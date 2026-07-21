"""Tests for the onboarding-reconcile catalog extension.

Two layers:
- Rendering: the shipped manifest renders against its example_config and passes
  every render-time rail (lint, role-allowlist closure, schedule validity), and
  the rendered artifact has the shape the install service will materialise.
- Behavior: the rendered Monty script executes in the real handler runtime with a
  stubbed emitter (get_role_members) and actor (add_role/remove_role), proving the
  delay gate, the pending/missing-join skips, the add-gated removal, and the
  bounded-batch cap that keeps the fire inside the 10/fire role-change budget.

Scoped to this extension only — imports its own manifest and exercises its own
rendered script; no whole-catalog scan.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smarter_dev.extensions.catalog.onboarding_reconcile import MANIFEST
from smarter_dev.extensions.rendering import (
    extract_granted_role_literals,
    render_bundle,
)
from smarter_dev.web.handler_budget import HandlerBudget, admin_budget
from smarter_dev.web.handler_runtime import run_handler_script

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/onboarding_reconcile"
)


def _scripts() -> dict[str, str]:
    return {
        handler.key: (_PACKAGE_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _rendered(config: dict | None = None):
    bundle = render_bundle(MANIFEST, config or MANIFEST.example_config, _scripts())
    assert len(bundle) == 1
    return bundle[0]


# -- rendering rails -----------------------------------------------------------


def test_example_config_renders_and_lints_clean():
    item = _rendered()
    assert item.name == "onboarding-reconcile"
    assert item.trigger_type == "schedule"
    # A guild-wide sweep with no channel to send to.
    assert item.channel_ids == []
    # The daily schedule timing key survives as a literal.
    assert item.settings["daily_time"] == "04:00"


def test_rendered_role_grants_are_within_the_allowlist():
    item = _rendered()
    granted = extract_granted_role_literals(item.script)
    # The script grants the full role and revokes the newcomer role.
    assert granted == {"644325811301777426", "888160821673349140"}
    # Every granted/revoked literal is in the host-enforced allowlist.
    assert granted <= set(item.settings["allowed_role_ids"])


def test_role_literals_are_baked_in_from_config():
    item = _rendered(
        {
            "newcomer_role_id": "111111111111111111",
            "full_member_role_id": "222222222222222222",
            "promotion_delay_hours": 72,
        }
    )
    assert 'add_role(member["member_id"], "222222222222222222"' in item.script
    assert 'remove_role(member["member_id"], "888' not in item.script
    assert "DELAY_HOURS = 72" in item.script
    assert set(item.settings["allowed_role_ids"]) == {
        "111111111111111111",
        "222222222222222222",
    }


def test_promotion_delay_hours_defaults_when_omitted():
    item = _rendered(
        {
            "newcomer_role_id": "111111111111111111",
            "full_member_role_id": "222222222222222222",
        }
    )
    assert "DELAY_HOURS = 48" in item.script


# -- runtime behavior ----------------------------------------------------------

_NEWCOMER = "888160821673349140"
_FULL = "644325811301777426"


@dataclass
class _FakeEmitter:
    """Only the surface the sweep touches: get_role_members."""

    role_members: dict = field(default_factory=dict)
    role_member_calls: list = field(default_factory=list)

    async def get_role_members(self, role_id: str) -> list:
        self.role_member_calls.append(role_id)
        return list(self.role_members.get(role_id, []))


@dataclass
class _FakeActor:
    calls: list = field(default_factory=list)
    # Member ids that model a member who has left (404): the role op returns False.
    gone: set = field(default_factory=set)

    async def add_role(self, user_id, role_id, reason=None):
        self.calls.append(("add_role", user_id, role_id, reason))
        return user_id not in self.gone

    async def remove_role(self, user_id, role_id, reason=None):
        self.calls.append(("remove_role", user_id, role_id, reason))
        return user_id not in self.gone


@dataclass
class _StubLimiter:
    allow: bool = True
    calls: list = field(default_factory=list)

    async def hit(self, key: str, limit: int, window_seconds: int | None = None) -> bool:
        self.calls.append((key, limit, window_seconds))
        return self.allow


def _member(member_id: str, *, days_ago: float, pending: bool = False,
            joined_at: str | None = "__auto__") -> dict:
    if joined_at == "__auto__":
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
        joined_at = when.isoformat()
    return {
        "member_id": member_id,
        "username": f"user{member_id}",
        "display_name": f"User {member_id}",
        "joined_at": joined_at,
        "account_created_at": "2020-01-01T00:00:00+00:00",
        "has_custom_avatar": False,
        "pending": pending,
    }


async def _fire(members: list[dict], *, actor=None, budget=None):
    item = _rendered()
    actor = actor or _FakeActor()
    emitter = _FakeEmitter(role_members={_NEWCOMER: members})
    limiter = _StubLimiter()
    result = await run_handler_script(
        item.script,
        {"trigger_type": "schedule"},
        channel_id="",
        guild_id="G1",
        emitter=emitter,
        limiter=limiter,
        budget=budget or admin_budget(),
        actor=actor,
        allowed_role_ids=list(item.settings["allowed_role_ids"]),
    )
    return result, actor, emitter


async def test_promotes_a_member_past_the_delay():
    result, actor, emitter = await _fire([_member("100", days_ago=3)])
    assert result.outcome == "ok", result.error
    assert emitter.role_member_calls == [_NEWCOMER]
    assert ("add_role", "100", _FULL, "onboarding: reconcile promotion") in actor.calls
    assert ("remove_role", "100", _NEWCOMER, "onboarding: reconcile promotion") in actor.calls
    assert result.usage["role_changes"] == 2


async def test_skips_member_not_yet_past_the_delay():
    result, actor, _ = await _fire([_member("100", days_ago=1)])
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert result.usage["role_changes"] == 0


async def test_skips_pending_member_even_when_overdue():
    result, actor, _ = await _fire([_member("100", days_ago=10, pending=True)])
    assert result.outcome == "ok", result.error
    assert actor.calls == []


async def test_skips_member_with_missing_joined_at():
    result, actor, _ = await _fire([_member("100", days_ago=0, joined_at=None)])
    assert result.outcome == "ok", result.error
    assert actor.calls == []


async def test_removal_is_gated_on_add_role_success():
    # The member left between the sweep read and the grant: add_role returns False,
    # so the newcomer role is NOT removed (silent no-op, one role-change spent).
    actor = _FakeActor(gone={"100"})
    result, actor, _ = await _fire([_member("100", days_ago=3)], actor=actor)
    assert result.outcome == "ok", result.error
    assert ("add_role", "100", _FULL, "onboarding: reconcile promotion") in actor.calls
    assert not any(call[0] == "remove_role" for call in actor.calls)
    assert result.usage["role_changes"] == 1


async def test_batch_is_bounded_to_stay_within_the_role_change_budget():
    # Seven overdue members, but the fire promotes at most five (10 role-changes =
    # the admin budget) and completes cleanly; the rest wait for the next run.
    members = [_member(str(i), days_ago=5) for i in range(7)]
    result, actor, _ = await _fire(members)
    assert result.outcome == "ok", result.error
    promoted = {c[1] for c in actor.calls if c[0] == "add_role"}
    assert len(promoted) == 5
    assert result.usage["role_changes"] == 10


async def test_all_gone_members_do_not_breach_the_budget():
    # Worst case for metering: every attempted member has left, so each promotion
    # spends only the add (1 role-change). The attempt is counted before the grant,
    # so five attempts spend five role-changes and the fire still completes cleanly.
    members = [_member(str(i), days_ago=5) for i in range(7)]
    actor = _FakeActor(gone={str(i) for i in range(7)})
    result, actor, _ = await _fire(members, actor=actor)
    assert result.outcome == "ok", result.error
    assert sum(1 for c in actor.calls if c[0] == "add_role") == 5
    assert not any(c[0] == "remove_role" for c in actor.calls)
    assert result.usage["role_changes"] == 5


async def test_empty_holding_role_is_a_clean_noop():
    result, actor, emitter = await _fire([])
    assert result.outcome == "ok", result.error
    assert actor.calls == []
    assert result.usage["role_changes"] == 0
    assert result.usage["discord_reads"] == 1  # the one get_role_members read
