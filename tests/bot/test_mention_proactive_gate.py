"""When a channel runs the proactive agent, the old chat bot is fully off.

No mention, reply, auto-respond, stop-heuristic or observe path of the
mention plugin may run in a proactive-enabled channel.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from smarter_dev.bot.plugins import mention
from smarter_dev.bot.services.proactive_settings_service import (
    ProactiveChannelSettings,
)

BOT_USER_ID = 999
CHANNEL_ID = 42
GUILD_ID = 7


def _settings_service(enabled: bool | None) -> SimpleNamespace:
    """``enabled=None`` means the lookup raises (API down)."""
    if enabled is None:
        return SimpleNamespace(
            get_settings=AsyncMock(side_effect=RuntimeError("api down"))
        )
    return SimpleNamespace(
        get_settings=AsyncMock(
            return_value=ProactiveChannelSettings(
                guild_id=str(GUILD_ID), channel_id=str(CHANNEL_ID),
                enabled=enabled, watch_addendum="",
            )
        )
    )


def _bot(proactive_service: SimpleNamespace) -> SimpleNamespace:
    rest = MagicMock()
    rest.create_message = AsyncMock()
    return SimpleNamespace(
        get_me=MagicMock(return_value=SimpleNamespace(id=BOT_USER_ID)),
        d={"proactive_settings_service": proactive_service},
        rest=rest,
    )


def _mention_event() -> SimpleNamespace:
    message = SimpleNamespace(
        id=555,
        author=SimpleNamespace(id=200, is_bot=False),
        created_at=datetime.now(UTC),
        user_mentions_ids=[BOT_USER_ID],
        referenced_message=None,
    )
    return SimpleNamespace(
        message=message, channel_id=CHANNEL_ID, guild_id=GUILD_ID,
        content=f"<@{BOT_USER_ID}> hello",
    )


async def _dispatch(event, bot):
    registry = SimpleNamespace(
        has_active=AsyncMock(return_value=False),
        get=AsyncMock(return_value=None),
    )
    memory = SimpleNamespace(increment_idle_counter=AsyncMock())
    activate = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch.object(mention.plugin, "_app", bot))
        stack.enter_context(
            patch.object(mention, "get_chat_engine_registry", return_value=registry)
        )
        stack.enter_context(
            patch.object(mention, "get_chat_memory", return_value=memory)
        )
        stack.enter_context(
            patch.object(mention, "_activate_engine", new=activate)
        )
        await mention.on_message_create(event)
    return SimpleNamespace(registry=registry, memory=memory, activate=activate)


async def test_proactive_channel_suppresses_the_old_chat_bot_entirely():
    result = await _dispatch(_mention_event(), _bot(_settings_service(True)))
    result.activate.assert_not_awaited()
    result.registry.has_active.assert_not_awaited()
    result.memory.increment_idle_counter.assert_not_awaited()


async def test_disabled_proactive_leaves_the_old_chat_bot_active():
    result = await _dispatch(_mention_event(), _bot(_settings_service(False)))
    result.activate.assert_awaited_once()


async def test_settings_lookup_failure_falls_back_to_the_old_chat_bot():
    result = await _dispatch(_mention_event(), _bot(_settings_service(None)))
    result.activate.assert_awaited_once()
