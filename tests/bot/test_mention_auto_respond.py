"""Tests for auto-respond channel activation in the mention plugin.

An admin can flag a channel's model override with ``auto_respond=True``, which
makes the bot treat a *plain* message (no @mention/reply) exactly like an
engagement — but only when no engine is already running and only through the
same stop/cooldown/rate-limit/message-limit gates the mention branch uses.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.plugins import mention
from smarter_dev.bot.services.models import ChannelModelOverride

BOT_USER_ID = 999
CHANNEL_ID = 42
GUILD_ID = 7


def _override(auto_respond: bool) -> ChannelModelOverride:
    return ChannelModelOverride(
        guild_id=str(GUILD_ID),
        channel_id=str(CHANNEL_ID),
        model_key="test-model",
        daily_token_budget=0,
        hourly_token_budget=0,
        auto_respond=auto_respond,
    )


def _fake_engine() -> SimpleNamespace:
    return SimpleNamespace(
        active=False,
        trigger_initial=MagicMock(),
        observe=AsyncMock(),
        fire_now=MagicMock(),
        shutdown=AsyncMock(),
    )


class _FakeRegistry:
    """Minimal stand-in for the process-global chat engine registry."""

    def __init__(self, *, active: bool, engine: SimpleNamespace) -> None:
        self._active = active
        self._engine = engine
        self.ensure_engine = AsyncMock(return_value=engine)

    async def has_active(self, channel_id: int) -> bool:
        return self._active

    async def get(self, channel_id: int) -> SimpleNamespace | None:
        return self._engine if self._active else None


def _override_service(auto_respond: bool | None) -> SimpleNamespace | None:
    """A service whose ``get_override`` returns an override, ``None`` or raises.

    ``auto_respond=None`` means "no override configured" (get returns ``None``).
    """
    override = None if auto_respond is None else _override(auto_respond)
    return SimpleNamespace(get_override=AsyncMock(return_value=override))


def _bot(override_service: SimpleNamespace | None) -> SimpleNamespace:
    data: dict = {}
    if override_service is not None:
        data["model_override_service"] = override_service
    rest = MagicMock()
    rest.create_message = AsyncMock()
    return SimpleNamespace(
        get_me=MagicMock(return_value=SimpleNamespace(id=BOT_USER_ID)),
        d=data,
        rest=rest,
    )


def _plain_message_event(content: str = "hello there") -> SimpleNamespace:
    message = SimpleNamespace(
        id=555,
        author=SimpleNamespace(id=200, is_bot=False),
        created_at=datetime.now(UTC),
        user_mentions_ids=[],
        referenced_message=None,
    )
    return SimpleNamespace(
        message=message,
        channel_id=CHANNEL_ID,
        guild_id=GUILD_ID,
        content=content,
    )


async def _dispatch(
    event: SimpleNamespace,
    bot: SimpleNamespace,
    registry: _FakeRegistry,
    *,
    memory: SimpleNamespace,
    rate_ok: bool = True,
    on_cooldown: bool = False,
    reject_over_limit: bool = False,
) -> None:
    """Run ``on_message_create`` with the collaborators fully stubbed out."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(mention.plugin, "_app", bot))
        stack.enter_context(
            patch.object(mention, "get_chat_engine_registry", return_value=registry)
        )
        stack.enter_context(
            patch.object(mention, "get_chat_memory", return_value=memory)
        )
        stack.enter_context(
            patch.object(
                mention.rate_limiter,
                "check_token_limit",
                return_value=rate_ok,
            )
        )
        stack.enter_context(
            patch.object(mention, "is_channel_on_cooldown", return_value=on_cooldown)
        )
        stack.enter_context(patch.object(mention, "set_channel_cooldown"))
        stack.enter_context(
            patch.object(
                mention,
                "_reject_when_over_limit",
                new=AsyncMock(return_value=reject_over_limit),
            )
        )
        await mention.on_message_create(event)


def _memory() -> SimpleNamespace:
    return SimpleNamespace(increment_idle_counter=AsyncMock())


@pytest.mark.asyncio
async def test_auto_respond_channel_activates_on_plain_message():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event()
    bot = _bot(_override_service(auto_respond=True))

    await _dispatch(event, bot, registry, memory=memory)

    registry.ensure_engine.assert_awaited_once()
    engine.trigger_initial.assert_called_once_with(event.message)
    memory.increment_idle_counter.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_auto_channel_does_not_activate():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event()
    bot = _bot(_override_service(auto_respond=False))

    await _dispatch(event, bot, registry, memory=memory)

    registry.ensure_engine.assert_not_awaited()
    engine.trigger_initial.assert_not_called()
    memory.increment_idle_counter.assert_awaited_once_with(CHANNEL_ID)


@pytest.mark.asyncio
async def test_channel_without_override_does_not_activate():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event()
    bot = _bot(_override_service(auto_respond=None))

    await _dispatch(event, bot, registry, memory=memory)

    registry.ensure_engine.assert_not_awaited()
    memory.increment_idle_counter.assert_awaited_once_with(CHANNEL_ID)


@pytest.mark.asyncio
async def test_stop_phrase_in_auto_channel_does_not_activate():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event(content="stop")
    bot = _bot(_override_service(auto_respond=True))

    await _dispatch(event, bot, registry, memory=memory)

    registry.ensure_engine.assert_not_awaited()
    engine.trigger_initial.assert_not_called()


@pytest.mark.asyncio
async def test_auto_respond_respects_cooldown():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event()
    bot = _bot(_override_service(auto_respond=True))

    await _dispatch(event, bot, registry, memory=memory, on_cooldown=True)

    registry.ensure_engine.assert_not_awaited()
    engine.trigger_initial.assert_not_called()


@pytest.mark.asyncio
async def test_auto_respond_drops_over_limit_user():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event()
    bot = _bot(_override_service(auto_respond=True))

    await _dispatch(
        event, bot, registry, memory=memory, reject_over_limit=True
    )

    registry.ensure_engine.assert_not_awaited()
    engine.trigger_initial.assert_not_called()


@pytest.mark.asyncio
async def test_override_lookup_failure_degrades_to_no_activation():
    engine = _fake_engine()
    registry = _FakeRegistry(active=False, engine=engine)
    memory = _memory()
    event = _plain_message_event()
    service = SimpleNamespace(
        get_override=AsyncMock(side_effect=RuntimeError("override backend down"))
    )
    bot = _bot(service)

    await _dispatch(event, bot, registry, memory=memory)

    registry.ensure_engine.assert_not_awaited()
    engine.trigger_initial.assert_not_called()
    memory.increment_idle_counter.assert_awaited_once_with(CHANNEL_ID)
