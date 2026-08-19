"""Tests for the proactive plugin: debounce math, conversion, wake flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    # Stored (legacy plain-text) addendum seeded the instruction store.
    assert any(
        entry.text == "stored addendum"
        for entry in wake_setup.captured_stores[0].entries
    )
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
        wake_setup.captured_stores[0].set_instruction(
            "watch for benchmarks", ttl_seconds=3600
        )
        return wake_setup.adapter.result

    wake_setup.adapter.activate = activate_and_update
    await proactive.run_wake(state)
    assert len(wake_setup.service.saved_addenda) == 1
    assert "watch for benchmarks" in wake_setup.service.saved_addenda[0]


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


# --- passive/active scheduling ----------------------------------------------


def test_engagement_detection_on_mention_and_reply_to_bot():
    assert proactive.event_engages_bot(
        _hikari_message(user_mentions_ids=(999,)), "999"
    )
    assert proactive.event_engages_bot(
        _hikari_message(
            referenced_message=SimpleNamespace(
                id=1, author=SimpleNamespace(id=999)
            )
        ),
        "999",
    )
    assert not proactive.event_engages_bot(_hikari_message(), "999")


@pytest.fixture
def listener_setup(wake_setup, monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        proactive, "_schedule_wake", lambda state: scheduled.append(state)
    )
    wake_setup.scheduled = scheduled
    return wake_setup


def _event(message):
    return SimpleNamespace(
        author=message.author,
        guild_id=2,
        channel_id=1,
        message=message,
    )


async def test_passive_message_buffers_without_arming_the_debounce(listener_setup):
    await proactive.on_guild_message(_event(_hikari_message()))
    state = listener_setup.runtime.states[1]
    assert len(state.buffer) == 1
    assert listener_setup.scheduled == []  # waits for the 15-min sweep


async def test_engagement_flips_to_active_and_arms_the_debounce(listener_setup):
    await proactive.on_guild_message(
        _event(_hikari_message(user_mentions_ids=(999,)))
    )
    state = listener_setup.runtime.states[1]
    assert state.active_until > proactive.time.monotonic()
    assert listener_setup.scheduled == [state]

    # Ordinary chatter during the active window also ingests fast.
    await proactive.on_guild_message(_event(_hikari_message(id=556)))
    assert listener_setup.scheduled == [state, state]


async def test_passive_sweep_drains_buffered_channels(listener_setup):
    await proactive.on_guild_message(_event(_hikari_message()))
    state = listener_setup.runtime.states[1]
    assert state.buffer

    woken = []

    async def fake_run_wake(target_state, passive=False):
        woken.append((target_state, passive))
        target_state.buffer.clear()

    original = proactive.run_wake
    proactive.run_wake = fake_run_wake
    try:
        await proactive._passive_sweep(listener_setup.runtime)
    finally:
        proactive.run_wake = original
    assert woken == [(state, False)]


# --- memory injection --------------------------------------------------------


def test_render_memory_block_composes_known_sections():
    note = SimpleNamespace(channel_name="#general", channel_id="1",
                            text="alice is benchmarking her parser")
    block = proactive.render_memory_block(
        long_term_memory="This guild loves rust.",
        long_term_updated_at=datetime(2026, 8, 17, tzinfo=UTC),
        notes=[note],
        topic="parser performance chat",
        channel_notes="carol prefers concise answers",
    )
    assert "GUILD MEMORY (dreamed 2026-08-17)" in block
    assert "alice is benchmarking" in block
    assert "parser performance chat" in block
    assert "carol prefers concise" in block


def test_render_memory_block_empty_when_nothing_known():
    assert proactive.render_memory_block(
        long_term_memory=None, long_term_updated_at=None, notes=(),
        topic=None, channel_notes=None,
    ) == ""


async def test_wake_injects_memory_hourly_not_per_wake(wake_setup, monkeypatch):
    captured_kwargs = []
    adapter = wake_setup.adapter

    def capturing_factory(**kwargs):
        captured_kwargs.append(kwargs)
        return adapter

    monkeypatch.setattr(proactive, "TwoPassAdapter", capturing_factory)

    async def fake_memory(run, state):
        return "YOUR MEMORY: the guild loves rust"

    monkeypatch.setattr(proactive, "load_memory_block", fake_memory)

    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )
    await proactive.run_wake(state)
    assert captured_kwargs[-1]["brief_preamble"].startswith("YOUR MEMORY")

    # A second wake inside the hour carries no memory block.
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=556))
    )
    await proactive.run_wake(state)
    assert captured_kwargs[-1]["brief_preamble"] == ""


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def scan_iter(self, match=None):
        import fnmatch

        for key in list(self.data):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


async def test_cursor_round_trips_with_guild_id():
    from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore

    store = ProactiveHistoryStore(_FakeRedis())
    assert await store.read_cursor(1) is None
    await store.write_cursor(1, guild_id="2", last_message_id="555")
    cursor = await store.read_cursor(1)
    assert cursor == {"guild_id": "2", "last_message_id": "555"}
    assert await store.cursor_channel_ids() == [1]


async def test_wake_advances_the_cursor_to_newest_new_message(persistence_setup):
    state = persistence_setup.runtime.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=555))
    )
    state.buffer.append(
        proactive.channel_message_from_hikari(_hikari_message(id=556))
    )
    await proactive.run_wake(state)
    cursor = await persistence_setup.store.read_cursor(1)
    assert cursor == {"guild_id": "2", "last_message_id": "556"}


async def test_recovery_wakes_enabled_channels_on_missed_messages(
    persistence_setup, monkeypatch
):
    store = persistence_setup.store
    await store.write_cursor(1, guild_id="2", last_message_id="500")

    fresh = datetime.now(UTC)
    missed = [
        _hikari_message(id=501, created_at=fresh),
        _hikari_message(id=502, created_at=fresh, author=SimpleNamespace(
            id=999, username="smarter-bot", global_name=None, is_bot=True
        )),  # bot-authored: excluded from catch-up
        _hikari_message(
            id=499, created_at=fresh - timedelta(hours=2)
        ),  # older than the catch-up age cap: excluded
    ]
    persistence_setup.bot.rest.fetch_messages = (
        lambda channel_id, after=None: _FakeIterator(missed)
    )
    woken = []

    async def fake_run_wake(state, passive=False):
        woken.append([m.id for m in state.buffer])
        state.buffer.clear()

    monkeypatch.setattr(proactive, "run_wake", fake_run_wake)
    await proactive._recover_channels(persistence_setup.runtime)
    assert woken == [["501"]]


async def test_recovery_skips_disabled_channels(persistence_setup, monkeypatch):
    await persistence_setup.store.write_cursor(
        1, guild_id="2", last_message_id="500"
    )
    persistence_setup.service.settings = ProactiveChannelSettings(
        guild_id="2", channel_id="1", enabled=False, watch_addendum="",
    )
    woken = []

    async def fake_run_wake(state, passive=False):
        woken.append(state)

    monkeypatch.setattr(proactive, "run_wake", fake_run_wake)
    await proactive._recover_channels(persistence_setup.runtime)
    assert woken == []


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
