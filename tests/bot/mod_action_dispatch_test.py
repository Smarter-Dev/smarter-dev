"""Tests for the bot-side mod_action trigger dispatch (§3.5)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


# --------------------------------------------------------------------------
# Short-term event log: the bot owning its own moderation in conversation
# --------------------------------------------------------------------------


class _BotStub:
    """Stands in for the lightbulb bot the recorder reads its handle from."""

    def __init__(self) -> None:
        self.d: dict = {}


@pytest.fixture
def dispatch_env(monkeypatch):
    """Silences the handler fire and captures every recorded guild event."""
    recorded: list[tuple[object, object]] = []

    async def capture_event(bot, event, **kwargs) -> None:
        recorded.append((bot, event))

    async def swallow_dispatch(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(mod_action_dispatch, "record_guild_event", capture_event)
    monkeypatch.setattr(mod_action_dispatch, "_dispatch", swallow_dispatch)
    return recorded


async def test_dispatch_mod_action_records_the_action_for_the_chat_agent(dispatch_env):
    bot = _BotStub()

    await dispatch_mod_action(
        _action(action_type="timeout", duration_seconds=600), bot=bot
    )

    assert len(dispatch_env) == 1
    recorded_bot, event = dispatch_env[0]
    assert recorded_bot is bot
    assert event.kind == "mod_action"
    assert event.guild_id == "G1"
    assert event.action == "timeout"
    assert event.target_username == "bob"
    assert event.moderator_username == "carol"
    assert event.reason == "scam"
    assert event.duration_seconds == 600
    assert event.channel_id == "C9"
    # The row's own timestamp, not the moment the dispatch happened to run.
    assert event.at == datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


async def test_dispatch_mod_action_records_a_manual_slash_action_with_its_source(
    dispatch_env,
):
    """A /timeout IS the bot's account acting — kept, with the mod attributed."""
    await dispatch_mod_action(_action(source="manual"), bot=_BotStub())

    assert dispatch_env[0][1].source == "manual"


async def test_dispatch_mod_action_never_records_an_audit_log_action(dispatch_env):
    """A human acted with their own account; "I banned…" would be a false claim."""
    await dispatch_mod_action(_action(source="audit_log"), bot=_BotStub())

    assert dispatch_env == []


async def test_dispatch_mod_action_records_nothing_without_a_bot(dispatch_env):
    await dispatch_mod_action(_action())

    assert dispatch_env == []


async def test_dispatch_mod_action_survives_a_recorder_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("redis exploded")

    async def swallow_dispatch(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(mod_action_dispatch, "record_guild_event", boom)
    monkeypatch.setattr(mod_action_dispatch, "_dispatch", swallow_dispatch)

    # Writing the memory down must never break the moderation command itself.
    await dispatch_mod_action(_action(), bot=_BotStub())
