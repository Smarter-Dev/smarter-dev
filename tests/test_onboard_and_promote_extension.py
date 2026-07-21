"""Tests for the onboard-and-promote catalog extension.

Two layers, both scoped to this one extension (the catalog-wide registry scan
lives in ``tests/test_extension_registry.py`` and is run by the final verifier):

* Rendering — load this extension's manifest + script directly, render it with
  ``example_config``, and assert the render pipeline (placeholder resolution,
  lint, role-allowlist closure) passes and produces the expected artifacts.
* Behavior — execute the rendered script through the Monty runtime with the
  shared handler-runtime stubs (fake actor / emitter / timer recorder) for the
  rules-acceptance grant, the timer promotion, and the member-gone no-op.
"""

from __future__ import annotations

import datetime as dt

import pytest

from smarter_dev.extensions.registry import _load_one
from smarter_dev.extensions.rendering import (
    extract_granted_role_literals,
    render_bundle,
)
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_runtime import run_handler_script
from tests.web.handler_runtime_test import (
    _FakeActor,
    _FakeEmitter,
    _StubLimiter,
    _TimerRecorder,
)

_MODULE = "smarter_dev.extensions.catalog.onboard_and_promote"
_NEWCOMER_ROLE = "888160821673349140"
_FULL_ROLE = "644325811301777426"


def _loaded():
    return _load_one(_MODULE)


def _rendered(config=None):
    loaded = _loaded()
    return render_bundle(
        loaded.manifest, config or loaded.manifest.example_config, loaded.scripts
    )[0]


# -- manifest + rendering ------------------------------------------------------


def test_manifest_shape():
    manifest = _loaded().manifest
    assert manifest.slug == "onboard-and-promote"
    assert len(manifest.handlers) == 1
    handler = manifest.handlers[0]
    assert handler.trigger_type == "member_rules_accepted"
    # A guild-scoped member event has no home channel.
    assert handler.channel_scope == []


def test_example_config_renders_and_lints_clean():
    # render_bundle runs the full render-time rail set (placeholder resolution,
    # leftover-marker sweep, lint, and role-allowlist closure); no raise = clean.
    handler = _rendered()
    assert handler.trigger_type == "member_rules_accepted"
    assert handler.channel_ids == []


def test_both_role_ids_baked_in_and_in_allowlist():
    handler = _rendered()
    # Both grantable roles appear as string literals in the script...
    assert f'"{_NEWCOMER_ROLE}"' in handler.script
    assert f'"{_FULL_ROLE}"' in handler.script
    # ...and both are mirrored into the host-enforced allowlist.
    assert handler.settings["allowed_role_ids"] == [_NEWCOMER_ROLE, _FULL_ROLE]
    # Closure: every granted literal is covered by the allowlist.
    granted = extract_granted_role_literals(handler.script)
    assert granted == {_NEWCOMER_ROLE, _FULL_ROLE}
    assert granted <= set(handler.settings["allowed_role_ids"])


def test_delay_hours_converted_to_seconds_expression():
    # 48 hours -> the in-script "48 * 3600" conversion (int, bare literal).
    assert "48 * 3600" in _rendered().script


def test_custom_delay_renders():
    config = {
        "newcomer_role_id": _NEWCOMER_ROLE,
        "full_member_role_id": _FULL_ROLE,
        "promotion_delay_hours": 72,
    }
    assert "72 * 3600" in _rendered(config).script


# -- behavior (rendered script executed in the Monty runtime) ------------------


async def _fire(context, *, gone=None, allowed=None, delay_config=None):
    handler = _rendered(delay_config)
    actor = _FakeActor(gone=set(gone or []))
    recorder = _TimerRecorder()
    result = await run_handler_script(
        handler.script,
        context,
        channel_id="",
        guild_id="G1",
        emitter=_FakeEmitter(),
        limiter=_StubLimiter(),
        budget=admin_budget(),
        actor=actor,
        allowed_role_ids=allowed or handler.settings["allowed_role_ids"],
        timer_scheduler=recorder,
        handler_id="H1",
    )
    return result, actor, recorder


async def test_rules_accepted_grants_newcomer_and_arms_timer():
    before = dt.datetime.now(dt.timezone.utc)
    result, actor, recorder = await _fire(
        {"trigger_type": "member_rules_accepted", "member_id": "U1"}
    )
    after = dt.datetime.now(dt.timezone.utc)
    assert result.outcome == "ok", result.error
    # The holding role is granted on acceptance.
    assert ("add_role", "U1", _NEWCOMER_ROLE, "onboarding: rules accepted") in actor.calls
    # No promotion happens yet.
    assert not any(c[2] == _FULL_ROLE for c in actor.calls if c[0] == "add_role")
    # Exactly one promotion timer armed, carrying the member id, ~48h out.
    assert result.usage["timers_scheduled"] == 1
    assert len(recorder.calls) == 1
    fire_at, refire = recorder.calls[0]
    assert refire["trigger_type"] == "timer"
    assert refire["payload"] == {"member_id": "U1"}
    delay = dt.timedelta(hours=48)
    assert before + delay <= fire_at <= after + delay


async def test_timer_promotes_then_removes_holding_role():
    result, actor, recorder = await _fire(
        {"trigger_type": "timer", "payload": {"member_id": "U2"}}
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [
        ("add_role", "U2", _FULL_ROLE, "onboarding: promotion"),
        ("remove_role", "U2", _NEWCOMER_ROLE, "onboarding: promotion"),
    ]
    # The timer branch must not arm another timer (no infinite re-fire).
    assert recorder.calls == []


async def test_timer_member_gone_skips_holding_role_removal():
    # add_role returns False for a departed member (404) -> the holding-role
    # removal is gated out; the fire still completes ok.
    result, actor, _ = await _fire(
        {"trigger_type": "timer", "payload": {"member_id": "GONE"}},
        gone=["GONE"],
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("add_role", "GONE", _FULL_ROLE, "onboarding: promotion")]
    assert not any(c[0] == "remove_role" for c in actor.calls)


async def test_custom_delay_arms_timer_at_configured_offset():
    before = dt.datetime.now(dt.timezone.utc)
    config = {
        "newcomer_role_id": _NEWCOMER_ROLE,
        "full_member_role_id": _FULL_ROLE,
        "promotion_delay_hours": 6,
    }
    _, _, recorder = await _fire(
        {"trigger_type": "member_rules_accepted", "member_id": "U3"},
        delay_config=config,
    )
    after = dt.datetime.now(dt.timezone.utc)
    fire_at, _ = recorder.calls[0]
    delay = dt.timedelta(hours=6)
    assert before + delay <= fire_at <= after + delay
