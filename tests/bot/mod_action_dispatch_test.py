"""Tests for the bot-side mod_action trigger dispatch (§3.5)."""

from __future__ import annotations

from datetime import datetime, timezone

from smarter_dev.bot import mod_action_dispatch
from smarter_dev.bot.mod_action_dispatch import (
    build_mod_action_context,
    dispatch_mod_action,
)
from smarter_dev.web.models import ModerationAction


def _action(**over) -> ModerationAction:
    fields = {
        "guild_id": "G1",
        "target_user_id": "U1",
        "target_username": "bob",
        "moderator_user_id": "MOD1",
        "moderator_username": "carol",
        "action_type": "ban",
        "reason": "scam",
        "duration_seconds": None,
        "source": "manual",
        "channel_id": "C9",
        "trigger_message_id": "M9",
        "created_at": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
    }
    fields.update(over)
    return ModerationAction(**fields)


def test_build_mod_action_context_maps_row():
    context = build_mod_action_context(_action())
    assert context == {
        "trigger_type": "mod_action",
        "action_type": "ban",
        "target_user_id": "U1",
        "target_username": "bob",
        "moderator_user_id": "MOD1",
        "moderator_username": "carol",
        "reason": "scam",
        "duration_seconds": None,
        "source": "manual",
        "channel_id": "C9",
        "trigger_message_id": "M9",
        "created_at": "2026-01-02T03:04:00+00:00",
    }


def test_build_mod_action_context_tolerates_unflushed_created_at():
    # A row whose server-default created_at hasn't been populated maps to None,
    # never an AttributeError.
    context = build_mod_action_context(_action(created_at=None))
    assert context["created_at"] is None


async def test_dispatch_mod_action_posts_expected_payload(monkeypatch):
    calls = []

    async def capture(channel_id, guild_id, trigger_type, context, **kwargs):
        calls.append((channel_id, guild_id, trigger_type, context))

    monkeypatch.setattr(mod_action_dispatch, "_dispatch", capture)

    await dispatch_mod_action(_action())

    assert len(calls) == 1
    channel_id, guild_id, trigger_type, context = calls[0]
    # Guild-scoped with NO home channel, guild taken from the row.
    assert channel_id == ""
    assert guild_id == "G1"
    assert trigger_type == "mod_action"
    assert context["action_type"] == "ban"


async def test_dispatch_mod_action_swallows_dispatch_error(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(mod_action_dispatch, "_dispatch", boom)

    # A dispatch failure must never propagate into the mod command.
    await dispatch_mod_action(_action())
