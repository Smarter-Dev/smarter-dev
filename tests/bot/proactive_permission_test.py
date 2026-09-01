"""The /proactive commands are gated on Manage Messages, not Administrator.

Moderators need to be able to switch the bot off in channels they moderate
without holding full admin.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hikari
import pytest

from smarter_dev.bot.plugins import admin_gate, proactive


def _context(permissions: hikari.Permissions) -> SimpleNamespace:
    member = object.__new__(hikari.InteractionMember)
    return SimpleNamespace(
        member=member,
        respond=AsyncMock(),
        guild_id=2,
        channel_id=1,
        _permissions=permissions,
    )


async def _denied(ctx) -> bool:
    with patch.object(
        admin_gate.lightbulb.utils,
        "permissions_for",
        return_value=ctx._permissions,
    ):
        return await proactive.deny_without_moderator_permissions(
            ctx, proactive.MODERATOR_DENIAL_MESSAGE
        )


async def test_manage_messages_is_allowed():
    ctx = _context(hikari.Permissions.MANAGE_MESSAGES)
    assert await _denied(ctx) is False
    ctx.respond.assert_not_awaited()


async def test_administrator_is_still_allowed():
    ctx = _context(hikari.Permissions.ADMINISTRATOR)
    assert await _denied(ctx) is False


async def test_plain_member_is_denied_ephemerally():
    ctx = _context(hikari.Permissions.SEND_MESSAGES)
    assert await _denied(ctx) is True
    ctx.respond.assert_awaited_once()
    kwargs = ctx.respond.await_args.kwargs
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "moderator" in ctx.respond.await_args.args[0].lower()


async def test_outside_a_guild_is_denied():
    ctx = SimpleNamespace(member=None, respond=AsyncMock())
    denied = await proactive.deny_without_moderator_permissions(
        ctx, proactive.MODERATOR_DENIAL_MESSAGE
    )
    assert denied is True
    assert "server" in ctx.respond.await_args.args[0].lower()


def test_moderator_check_accepts_either_permission():
    assert proactive.has_moderator_permissions(
        hikari.Permissions.MANAGE_MESSAGES
    )
    assert proactive.has_moderator_permissions(hikari.Permissions.ADMINISTRATOR)
    assert not proactive.has_moderator_permissions(
        hikari.Permissions.SEND_MESSAGES | hikari.Permissions.ADD_REACTIONS
    )


# --- enabling a channel wakes the guild agent --------------------------------


def _enable_context() -> SimpleNamespace:
    member = object.__new__(hikari.InteractionMember)
    return SimpleNamespace(
        member=member,
        respond=AsyncMock(),
        guild_id=2,
        channel_id=1,
    )


class _ToggleSettingsService:
    def __init__(self, enabled: bool):
        self._enabled = enabled
        self.set_calls = []

    async def get_settings(self, guild_id, channel_id):
        return SimpleNamespace(enabled=self._enabled)

    async def set_enabled(self, guild_id, channel_id, enabled):
        self.set_calls.append((guild_id, channel_id, enabled))
        self._enabled = enabled


@pytest.fixture
def toggle_setup(monkeypatch):
    runtime = proactive.ProactiveRuntime(
        SimpleNamespace(
            cache=SimpleNamespace(
                get_guild_channel=lambda _cid: SimpleNamespace(name="general")
            ),
            get_me=lambda: None,
        ),
        start_consumers=False,
    )
    monkeypatch.setattr(proactive, "runtime", runtime)
    monkeypatch.setattr(
        admin_gate.lightbulb.utils,
        "permissions_for",
        lambda member: hikari.Permissions.MANAGE_MESSAGES,
    )
    return runtime


async def test_enabling_a_new_channel_wakes_the_guild_agent(toggle_setup):
    service = _ToggleSettingsService(enabled=False)
    toggle_setup.bot.d = {"proactive_settings_service": service}
    monkeypatch_service = lambda: service
    toggle_setup.settings_service = monkeypatch_service

    await proactive._set_enabled(_enable_context(), True)

    queue = toggle_setup.guild_state_for(2).queue
    kinds = [n.kind for n in queue.items]
    assert kinds == ["channel_enabled"]
    enabled_notification = queue.items[0]
    assert enabled_notification.wakes is True
    assert enabled_notification.channel_id == "1"
    assert enabled_notification.channel_name == "general"


async def test_reenabling_an_enabled_channel_does_not_wake(toggle_setup):
    service = _ToggleSettingsService(enabled=True)
    toggle_setup.settings_service = lambda: service

    await proactive._set_enabled(_enable_context(), True)

    assert toggle_setup.guild_state_for(2).queue.items == []


async def test_disabling_never_wakes(toggle_setup):
    service = _ToggleSettingsService(enabled=True)
    toggle_setup.settings_service = lambda: service

    await proactive._set_enabled(_enable_context(), False)

    assert toggle_setup.guild_state_for(2).queue.items == []
