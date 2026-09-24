"""Both chat triggers reach Discord when the agent returns a real run result.

Regression for #3: after the Pydantic AI 2.x upgrade ``AgentRunResult.usage``
became a property, but the engine still called ``result.usage()``. Every other
engine test hands back a ``SimpleNamespace`` fake, so the stale call passed CI
while production crashed after the model answered and never sent a reply.

These tests return a genuine ``pydantic_ai.run.AgentRunResult`` and drive a
real ``ChannelEngine`` from the mention plugin's ``on_message_create``, once
for an @mention and once for a plain message in an auto-respond channel.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RunUsage

from smarter_dev.bot.plugins import mention
from smarter_dev.bot.services.models import ChannelModelOverride
from tests.bot.services.test_chat_engine import _build_engine
from tests.bot.services.test_chat_engine import _patch_engine
from tests.bot.services.test_chat_engine import _send

BOT_USER_ID = 999
REPLY = "restored reply"


def test_run_result_usage_is_a_property():
    """The contract the engine relies on: ``usage`` is read, not called."""
    result = AgentRunResult(output="x")
    assert isinstance(result.usage, RunUsage)
    assert not callable(result.usage)


class _Registry:
    """Hands the mention plugin one real engine for the channel."""

    def __init__(self, engine) -> None:
        self.engine = engine

    async def has_active(self, channel_id: int) -> bool:
        return False

    async def get(self, channel_id: int):
        return None

    async def ensure_engine(self, **kwargs):
        return self.engine


def _event(*, mentions_bot: bool) -> SimpleNamespace:
    message = SimpleNamespace(
        id=9001,
        author=SimpleNamespace(id=200, is_bot=False, username="alice"),
        created_at=datetime.now(UTC),
        user_mentions_ids=[BOT_USER_ID] if mentions_bot else [],
        referenced_message=None,
    )
    return SimpleNamespace(
        message=message, channel_id=42, guild_id=99, content="hello there"
    )


def _auto_respond_service() -> SimpleNamespace:
    override = ChannelModelOverride(
        guild_id="99",
        channel_id="42",
        model_key=None,
        daily_token_budget=0,
        hourly_token_budget=0,
        auto_respond=True,
    )
    return SimpleNamespace(
        get_override_or_last_known=AsyncMock(return_value=override)
    )


async def _dispatch_and_run(bot, memory, event) -> list[bool]:
    runs: list[bool] = []

    async def real_result_run(**kwargs):
        runs.append(True)
        return AgentRunResult(output=_send(REPLY, topic="greeting"))

    engine_patches = _patch_engine(agent_run=real_result_run, fake_memory=memory)
    with ExitStack() as stack:
        for p in engine_patches:
            stack.enter_context(p)
        engine, _ = await _build_engine(bot)
        registry = _Registry(engine)
        stack.enter_context(patch.object(mention.plugin, "_app", bot))
        stack.enter_context(
            patch.object(mention, "get_chat_engine_registry", return_value=registry)
        )
        stack.enter_context(patch.object(mention, "get_chat_memory", return_value=memory))
        stack.enter_context(
            patch.object(mention.rate_limiter, "check_token_limit", return_value=True)
        )
        stack.enter_context(
            patch.object(mention, "is_channel_on_cooldown", return_value=False)
        )
        stack.enter_context(
            patch.object(
                mention, "_reject_when_over_limit", new=AsyncMock(return_value=False)
            )
        )
        stack.enter_context(
            patch.object(mention, "_record_engaged_message", new=AsyncMock())
        )
        engine.start()
        await mention.on_message_create(event)
        await asyncio.sleep(0.1)
        await engine.shutdown()
    return runs


def _bot(data: dict) -> MagicMock:
    bot = MagicMock()
    bot.get_me = MagicMock(return_value=SimpleNamespace(id=BOT_USER_ID))
    bot.d = data
    bot.rest.create_message = AsyncMock()
    return bot


def _memory() -> MagicMock:
    """The ChatMemory methods the mention plugin and the engine touch."""
    memory = MagicMock()
    for name in (
        "increment_idle_counter",
        "reset_idle_counter",
        "write_topic",
        "write_notes",
        "clear_notes",
        "write_history",
        "clear_history",
    ):
        setattr(memory, name, AsyncMock())
    memory.read_history = AsyncMock(return_value=[])
    memory.topic_for_activation = AsyncMock(return_value=None)
    memory.get_notes = AsyncMock(return_value=None)
    return memory


def _sent_texts(bot) -> list[str]:
    return [
        call.args[1] if len(call.args) > 1 else call.kwargs.get("content")
        for call in bot.rest.create_message.await_args_list
    ]


async def test_mention_reply_is_sent_from_a_real_run_result():
    bot = _bot({})

    runs = await _dispatch_and_run(bot, _memory(), _event(mentions_bot=True))

    assert runs == [True]
    assert REPLY in _sent_texts(bot)


async def test_auto_respond_reply_is_sent_from_a_real_run_result():
    bot = _bot({"model_override_service": _auto_respond_service()})

    runs = await _dispatch_and_run(bot, _memory(), _event(mentions_bot=False))

    assert runs == [True]
    assert REPLY in _sent_texts(bot)
