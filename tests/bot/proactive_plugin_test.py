"""Tests for the proactive plugin: debounce math, conversion, wake flow."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.types import (
    ActivationResult,
    ProposedReaction,
    ProposedResponse,
)
from smarter_dev.bot.services.proactive_settings_service import (
    ProactiveChannelSettings,
)


# --- debounce math -----------------------------------------------------------


def test_fire_delay_uses_quiet_gap():
    # Last message 5s ago: fire 10s from now (15s quiet).
    assert proactive.compute_fire_delay(100.0, 110.0, 115.0) == 10.0


def test_fire_delay_caps_at_max_wait_from_first():
    # Burst running 55s: the 60s cap wins over last+15.
    assert proactive.compute_fire_delay(100.0, 155.0, 155.0) == 5.0


def test_fire_delay_never_negative():
    assert proactive.compute_fire_delay(0.0, 0.0, 500.0) == 0.0


# --- hikari message conversion -----------------------------------------------


def _hikari_message(**overrides):
    fields = {
        "id": 555,
        "created_at": datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        "author": SimpleNamespace(
            id=901, username="alice", global_name="Alice", is_bot=False
        ),
        "member": SimpleNamespace(nickname="ally"),
        "content": "hey there",
        "type": 0,
        "referenced_message": None,
        "user_mentions_ids": (777,),
        "mentions_everyone": False,
        "attachments": [1],
        "stickers": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_channel_message_conversion_prefers_nickname_and_stringifies_ids():
    converted = proactive.channel_message_from_hikari(_hikari_message())
    assert converted.id == "555"
    assert converted.author_display == "ally"
    assert converted.mention_user_ids == ("777",)
    assert converted.attachment_count == 1
    assert converted.reply_to_id is None
    assert converted.is_bot is False


def test_channel_message_conversion_reads_reply_reference():
    converted = proactive.channel_message_from_hikari(
        _hikari_message(
            referenced_message=SimpleNamespace(id=444), member=None
        )
    )
    assert converted.reply_to_id == "444"
    assert converted.author_display == "Alice"  # global_name fallback


# --- wake flow ---------------------------------------------------------------


class _StubAdapter:
    def __init__(self, result: ActivationResult):
        self.result = result
        self.contexts = []

    async def activate(self, context):
        self.contexts.append(context)
        return self.result


class _StubSettingsService:
    def __init__(self, enabled: bool = True, addendum: str = ""):
        self.settings = ProactiveChannelSettings(
            guild_id="2", channel_id="1", enabled=enabled,
            watch_addendum=addendum,
        )
        self.saved_addenda: list[str] = []

    async def get_settings(self, guild_id, channel_id):
        return self.settings

    async def set_watch_addendum(self, guild_id, channel_id, addendum):
        self.saved_addenda.append(addendum)
        return self.settings


class _FakeIterator:
    def __init__(self, messages):
        self._messages = messages

    def limit(self, n):
        return self

    def __aiter__(self):
        self._iter = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _fake_bot(history_messages, service):
    rest = SimpleNamespace(
        fetch_messages=lambda channel_id: _FakeIterator(history_messages),
        create_message=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    return SimpleNamespace(
        rest=rest,
        d={"proactive_settings_service": service},
        get_me=lambda: SimpleNamespace(id=999, username="smarter-bot"),
        cache=SimpleNamespace(
            get_guild_channel=lambda cid: SimpleNamespace(name="general"),
            get_guild=lambda gid: SimpleNamespace(name="Smarter Dev"),
        ),
    )


@pytest.fixture
def wake_setup(monkeypatch):
    service = _StubSettingsService(addendum="stored addendum")
    history = [_hikari_message(id=100 + n) for n in range(3)]
    bot = _fake_bot(history, service)
    runtime = proactive.ProactiveRuntime(bot)
    monkeypatch.setattr(proactive, "runtime", runtime)
    # The adapter is stubbed below, but its constructor arguments are still
    # evaluated — keep the real model builders out of the test path.
    monkeypatch.setattr(runtime, "watcher", lambda: None)
    monkeypatch.setattr(runtime, "skim", lambda: None)
    monkeypatch.setattr(runtime, "agent_runner_for", lambda state: None)

    result = ActivationResult(
        responses=[ProposedResponse(reply_to_id="555", content="happy to help")],
        input_tokens=10, output_tokens=1, cache_read_tokens=0,
        model_id="stub",
        reactions=(ProposedReaction(message_id="555", emoji="👍"),),
        details={"watcher": {"wake": True}},
    )
    adapter = _StubAdapter(result)
    captured_stores = []

    def fake_adapter_factory(**kwargs):
        captured_stores.append(kwargs["instruction_store"])
        return adapter

    monkeypatch.setattr(proactive, "TwoPassAdapter", fake_adapter_factory)
    return SimpleNamespace(
        service=service, bot=bot, runtime=runtime, adapter=adapter,
        captured_stores=captured_stores,
    )


async def test_wake_drains_buffer_dispatches_and_seeds_addendum(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )

    await proactive.run_wake(state)

    assert state.buffer == []
    # Adapter saw the buffered message as new, fetched history excludes it.
    context = wake_setup.adapter.contexts[0]
    assert [m.id for m in context.new_messages] == ["555"]
    assert all(m.id != "555" for m in context.history)
    assert context.bot_user_id == "999"
    # Stored addendum seeded the instruction store.
    assert wake_setup.captured_stores[0].addendum == "stored addendum"
    # Dispatch: reply + reaction hit the rest API.
    wake_setup.bot.rest.create_message.assert_awaited_once()
    args, kwargs = wake_setup.bot.rest.create_message.await_args
    assert args[0] == 1 and args[1] == "happy to help"
    assert kwargs["reply"] == 555
    wake_setup.bot.rest.add_reaction.assert_awaited_once_with(1, 555, "👍")
    # No instruction update happened, so nothing was persisted.
    assert wake_setup.service.saved_addenda == []


async def test_wake_persists_updated_addendum(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )

    async def activate_and_update(context):
        wake_setup.captured_stores[0].update("watch for benchmarks")
        return wake_setup.adapter.result

    wake_setup.adapter.activate = activate_and_update
    await proactive.run_wake(state)
    assert wake_setup.service.saved_addenda == ["watch for benchmarks"]


async def test_wake_skips_when_channel_disabled(wake_setup):
    wake_setup.service.settings = ProactiveChannelSettings(
        guild_id="2", channel_id="1", enabled=False, watch_addendum="",
    )
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )
    await proactive.run_wake(state)
    assert wake_setup.adapter.contexts == []
    wake_setup.bot.rest.create_message.assert_not_awaited()


async def test_empty_non_passive_wake_is_a_noop(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    await proactive.run_wake(state)
    assert wake_setup.adapter.contexts == []


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


async def test_history_store_round_trips_and_survives_garbage():
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore

    store = ProactiveHistoryStore(_FakeRedis())
    assert await store.read(1) == []

    history = [ModelRequest(parts=[UserPromptPart("wake one")])]
    await store.write(1, history)
    loaded = await store.read(1)
    assert "wake one" in str(loaded[0])

    store._redis.data[ProactiveHistoryStore._history_key(1)] = b"not json"
    assert await store.read(1) == []


@pytest.fixture
def persistence_setup(wake_setup, monkeypatch):
    from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore

    store = ProactiveHistoryStore(_FakeRedis())
    monkeypatch.setattr(
        wake_setup.runtime, "history_store", lambda: store
    )
    runner = SimpleNamespace(history=["fresh"])
    monkeypatch.setattr(
        wake_setup.runtime, "agent_runner_for", lambda state: runner
    )
    wake_setup.store = store
    wake_setup.runner = runner
    return wake_setup


async def test_wake_loads_and_persists_agent_history(persistence_setup):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    stored = [ModelRequest(parts=[UserPromptPart("earlier wake")])]
    await persistence_setup.store.write(1, stored)

    state = persistence_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )

    async def activate_leaving_history(context):
        # The runner would normally append the turn; simulate that.
        persistence_setup.runner.history = list(
            persistence_setup.runner.history
        ) + [ModelRequest(parts=[UserPromptPart("this wake")])]
        return persistence_setup.adapter.result

    persistence_setup.adapter.activate = activate_leaving_history
    # The stubbed adapter factory must still expose activate.
    await proactive.run_wake(state)

    # Stored history was loaded into the runner before the wake…
    assert "earlier wake" in str(persistence_setup.runner.history[0])
    # …and the post-wake history (with the new turn) was persisted.
    persisted = await persistence_setup.store.read(1)
    assert any("this wake" in str(m) for m in persisted)
    assert state.history_loaded is True


async def test_wake_posts_generated_images(persistence_setup):
    from smarter_dev.bot.agents.chat_tools import GeneratedImage

    state = persistence_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )
    captured_factory = {}

    original_factory = proactive.TwoPassAdapter

    def factory_with_images(**kwargs):
        captured_factory["deps_factory"] = kwargs["deps_factory"]
        return persistence_setup.adapter

    # wake_setup already stubbed TwoPassAdapter; re-stub to capture the factory.
    import tests.bot.proactive_plugin_test  # noqa: F401 — same module

    proactive.TwoPassAdapter = factory_with_images
    try:
        async def activate_generating_image(context):
            deps = captured_factory["deps_factory"](
                env=None, actions=None, instruction_store=None,
                skim_transcript=None, budget=None,
            )
            deps.pending_images.append(
                GeneratedImage(data=b"png", mime_type="image/png",
                               filename="art.png")
            )
            return persistence_setup.adapter.result

        persistence_setup.adapter.activate = activate_generating_image
        await proactive.run_wake(state)
    finally:
        proactive.TwoPassAdapter = original_factory

    calls = persistence_setup.bot.rest.create_message.await_args_list
    # First the text reply, then the image message with attachments.
    assert len(calls) == 2
    image_kwargs = calls[1].kwargs
    assert len(image_kwargs["attachments"]) == 1
    assert image_kwargs["reply"] == 555
