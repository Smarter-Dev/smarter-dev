"""Tests for the privileged-routines slash-command pure helpers."""

from __future__ import annotations

import hikari

from smarter_dev.bot.plugins.privileged_routines import (
    build_action,
    build_settings,
    is_admin,
)


def test_is_admin():
    assert is_admin(hikari.Permissions.ADMINISTRATOR) is True
    assert is_admin(hikari.Permissions.MODERATE_MEMBERS) is False
    assert is_admin(hikari.Permissions.ADMINISTRATOR | hikari.Permissions.BAN_MEMBERS)


def test_build_action_timeout():
    action = build_action("timeout", "U1", 5, "C1", None, "spam")
    assert action == {
        "kind": "timeout",
        "target_user_id": "U1",
        "duration_seconds": 300,
        "reason": "spam",
    }


def test_build_action_delete():
    action = build_action("delete", None, None, "C1", "M1", None)
    assert action == {"kind": "delete", "channel_id": "C1", "message_id": "M1"}


def test_build_settings_timer_and_schedule():
    assert build_settings("timer", 10, None) == {"delay_seconds": 600}
    assert build_settings("schedule", 5, None) == {"interval_seconds": 300}
    assert build_settings("schedule", None, "08:00") == {"daily_time": "08:00"}
