"""Tests for the proactive plugin: debounce math, conversion, wake flow."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.adapter import AgentConsumer
from smarter_dev.bot.proactive.adapter import WatcherProducer
from smarter_dev.bot.proactive.notifications import watcher_summary_notification
from smarter_dev.bot.proactive.types import ActivationResult
from smarter_dev.bot.proactive.types import ProposedReaction
from smarter_dev.bot.proactive.types import ProposedResponse
from smarter_dev.bot.services.proactive_settings_service import ProactiveChannelSettings

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
        "member": SimpleNamespace(
            nickname="ally",
            get_roles=lambda: [SimpleNamespace(name="Regular")],
        ),
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
    assert converted.roles == ("Regular",)


def test_channel_message_conversion_reads_reply_reference():
    converted = proactive.channel_message_from_hikari(
        _hikari_message(referenced_message=SimpleNamespace(id=444), member=None)
    )
    assert converted.reply_to_id == "444"
    assert converted.author_display == "Alice"  # global_name fallback


# --- wake flow ---------------------------------------------------------------


class _StubProducer:
    def __init__(self, queue):
        self.queue = queue
        self.contexts = []
        self.details = {"watcher": {"wake": True}}
        self.wake_produced = True
        self.usage_by_model = {}

    async def produce(self, context):
        self.contexts.append(context)
        self.queue.push(
            watcher_summary_notification(
                summary="interesting activity",
                message_ids=[message.id for message in context.new_messages],
                wake=True,
                created_at=context.activated_at,
            )
        )
        return self.usage_by_model


class _StubConsumer:
    def __init__(self, result: ActivationResult):
        self.result = result
        self.contexts = []
        self.queue = None

    async def consume(self, context):
        self.contexts.append(context)
        self.queue.drain()
        return self.result


class _StubSettingsService:
    def __init__(self, enabled: bool = True, addendum: str = ""):
        self.settings = ProactiveChannelSettings(
            guild_id="2",
            channel_id="1",
            enabled=enabled,
            watch_addendum=addendum,
        )
        self.saved_addenda: list[str] = []
        self.recorded_usage: list[dict] = []
        self.usage_error: Exception | None = None

    async def get_settings(self, guild_id, channel_id):
        return self.settings

    async def set_watch_addendum(self, guild_id, channel_id, addendum):
        self.saved_addenda.append(addendum)
        return self.settings

    async def record_wake_usage(
        self,
        guild_id,
        channel_id,
        *,
        wake_id,
        metered_at,
        passive,
        responses,
        entries,
    ):
        if self.usage_error is not None:
            raise self.usage_error
        self.recorded_usage.append(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "wake_id": wake_id,
                "metered_at": metered_at,
                "passive": passive,
                "responses": responses,
                "entries": entries,
            }
        )


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
            raise StopAsyncIteration from None


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
    runtime = proactive.ProactiveRuntime(bot, start_consumers=False)
    monkeypatch.setattr(proactive, "runtime", runtime)
    # The split factories still evaluate their model dependencies.
    monkeypatch.setattr(runtime, "watcher", lambda: None)
    monkeypatch.setattr(runtime, "skim", lambda: None)
    monkeypatch.setattr(runtime, "agent_runner_for", lambda state: None)

    result = ActivationResult(
        responses=[ProposedResponse(reply_to_id="555", content="happy to help")],
        input_tokens=10,
        output_tokens=1,
        cache_read_tokens=0,
        model_id="stub",
        reactions=(ProposedReaction(message_id="555", emoji="👍"),),
        details={"watcher": {"wake": True}},
    )
    consumer = _StubConsumer(result)
    producers = []
    producer_usage = {}
    producer_stores = []
    captured_stores = []
    captured_kwargs = []

    def fake_producer_factory(**kwargs):
        producer = _StubProducer(kwargs["notification_queue"])
        producer.usage_by_model = dict(producer_usage)
        producers.append(producer)
        producer_stores.append(kwargs["instruction_store"])
        return producer

    def fake_consumer_factory(**kwargs):
        captured_stores.append(kwargs["instruction_store"])
        captured_kwargs.append(kwargs)
        consumer.queue = kwargs["notification_queue"]
        return consumer

    monkeypatch.setattr(proactive, "WatcherProducer", fake_producer_factory)
    monkeypatch.setattr(proactive, "AgentConsumer", fake_consumer_factory)
    return SimpleNamespace(
        service=service,
        bot=bot,
        runtime=runtime,
        consumer=consumer,
        producers=producers,
        producer_usage=producer_usage,
        producer_stores=producer_stores,
        captured_stores=captured_stores,
        captured_kwargs=captured_kwargs,
    )


async def _produce_and_consume(state):
    await proactive._run_producer(state)
    if state.queue.items:
        await proactive._consume_channel_once(state)


async def test_wake_drains_buffer_dispatches_and_seeds_addendum(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await _produce_and_consume(state)

    assert state.buffer == []
    # Adapter saw the buffered message as new, fetched history excludes it.
    context = wake_setup.producers[0].contexts[0]
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
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    async def activate_and_update(context):
        wake_setup.captured_stores[0].set_instruction(
            "watch for benchmarks", ttl_seconds=3600
        )
        return wake_setup.consumer.result

    wake_setup.consumer.consume = activate_and_update
    await _produce_and_consume(state)
    assert len(wake_setup.service.saved_addenda) == 1
    assert "watch for benchmarks" in wake_setup.service.saved_addenda[0]


async def test_producer_and_consumer_read_fresh_instruction_settings(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await proactive._run_producer(state)
    wake_setup.service.settings = ProactiveChannelSettings(
        guild_id="2",
        channel_id="1",
        enabled=True,
        watch_addendum="consumer-only addendum",
    )
    await proactive._consume_channel_once(state)

    assert any(
        entry.text == "stored addendum"
        for entry in wake_setup.producer_stores[0].entries
    )
    assert all(
        entry.text != "consumer-only addendum"
        for entry in wake_setup.producer_stores[0].entries
    )
    assert any(
        entry.text == "consumer-only addendum"
        for entry in wake_setup.captured_stores[0].entries
    )


async def test_wake_skips_when_channel_disabled(wake_setup):
    wake_setup.service.settings = ProactiveChannelSettings(
        guild_id="2",
        channel_id="1",
        enabled=False,
        watch_addendum="",
    )
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))
    await proactive._run_producer(state)
    assert wake_setup.producers == []
    assert wake_setup.consumer.contexts == []
    wake_setup.bot.rest.create_message.assert_not_awaited()


async def test_empty_non_passive_wake_is_a_noop(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    await proactive._run_producer(state)
    assert wake_setup.producers == []
    assert wake_setup.consumer.contexts == []


def _usage_result(**overrides) -> ActivationResult:
    fields = {
        "responses": [ProposedResponse(reply_to_id="555", content="happy to help")],
        "input_tokens": 1200,
        "output_tokens": 40,
        "cache_read_tokens": 100,
        "model_id": "gemini-3.7-flash",
        "usage_by_model": {
            "z-ai/glm-5.3-flash": {
                "input_tokens": 1000,
                "output_tokens": 30,
                "cache_read_tokens": 100,
            },
            "gemini-3.7-flash": {
                "input_tokens": 200,
                "output_tokens": 10,
                "cache_read_tokens": 0,
            },
        },
    }
    fields.update(overrides)
    return ActivationResult(**fields)


async def test_wake_persists_usage_per_model(wake_setup):
    wake_setup.producer_usage["z-ai/glm-5.3-flash"] = {
        "input_tokens": 1000,
        "output_tokens": 30,
        "cache_read_tokens": 100,
    }
    wake_setup.consumer.result = _usage_result(
        usage_by_model={
            "gemini-3.7-flash": {
                "input_tokens": 200,
                "output_tokens": 10,
                "cache_read_tokens": 0,
            }
        }
    )
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await _produce_and_consume(state)

    assert len(wake_setup.service.recorded_usage) == 2
    watcher_record, agent_record = wake_setup.service.recorded_usage
    assert watcher_record["guild_id"] == "2"
    assert watcher_record["channel_id"] == "1"
    assert watcher_record["wake_id"] != agent_record["wake_id"]
    assert watcher_record["passive"] is False
    assert watcher_record["responses"] == 0
    watcher = watcher_record["entries"][0]
    assert watcher["operation"] == "watcher"
    assert watcher["input_tokens"] == 1000
    assert watcher["output_tokens"] == 30
    assert watcher["cache_read_tokens"] == 100
    assert agent_record["responses"] == 1
    agent = agent_record["entries"][0]
    assert agent["operation"] == "agent"
    assert agent["input_tokens"] == 200


async def test_wake_without_usage_records_nothing(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await _produce_and_consume(state)

    assert wake_setup.service.recorded_usage == []


async def test_wake_survives_usage_persistence_failure(wake_setup):
    wake_setup.consumer.result = _usage_result()
    wake_setup.service.usage_error = RuntimeError("api down")
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await _produce_and_consume(state)

    # The response still went out even though usage persistence failed.
    wake_setup.bot.rest.create_message.assert_awaited_once()


# --- passive/active scheduling ----------------------------------------------


def test_engagement_detection_on_mention_and_reply_to_bot():
    assert proactive.event_engages_bot(_hikari_message(user_mentions_ids=(999,)), "999")
    assert proactive.event_engages_bot(
        _hikari_message(
            referenced_message=SimpleNamespace(id=1, author=SimpleNamespace(id=999))
        ),
        "999",
    )
    assert not proactive.event_engages_bot(_hikari_message(), "999")


@pytest.fixture
def listener_setup(wake_setup, monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        proactive, "_schedule_producer", lambda state: scheduled.append(state)
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
    await proactive.on_guild_message(_event(_hikari_message(user_mentions_ids=(999,))))
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

    produced = []

    async def fake_run_producer(target_state, passive=False):
        produced.append((target_state, passive))
        target_state.buffer.clear()

    original = proactive._run_producer
    proactive._run_producer = fake_run_producer
    try:
        await proactive._passive_sweep(listener_setup.runtime)
    finally:
        proactive._run_producer = original
    assert produced == [(state, True)]


# --- independent producer and consumer state ---------------------------------


async def test_mention_queues_verbatim_while_consumer_is_busy(listener_setup):
    state = listener_setup.runtime.state_for(2, 1)

    await proactive.on_guild_message(_event(_hikari_message(user_mentions_ids=(999,))))

    assert listener_setup.scheduled == [state]
    mention = next(n for n in state.queue.items if n.kind == "mention")
    assert "hey there" in mention.body
    assert mention.wakes is True


async def test_plain_active_message_buffers_for_the_producer(listener_setup):
    state = listener_setup.runtime.state_for(2, 1)
    state.active_until = proactive.time.monotonic() + 600

    await proactive.on_guild_message(_event(_hikari_message()))

    assert state.queue.items == []
    assert [message.id for message in state.buffer] == ["555"]
    assert listener_setup.scheduled == [state]


async def test_producer_runs_while_agent_consumer_is_mid_wake(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(_hikari_message(id=554))
        )
    )
    first_started = asyncio.Event()
    release = asyncio.Event()
    original_consume = wake_setup.consumer.consume

    async def blocking_consume(context):
        first_started.set()
        await release.wait()
        return await original_consume(context)

    wake_setup.consumer.consume = blocking_consume
    consumer_task = asyncio.create_task(proactive._consume_channel_once(state))
    await first_started.wait()

    await proactive._run_producer(state)

    reviewed = wake_setup.producers[0].contexts[0].new_messages
    assert [message.id for message in reviewed] == ["555"]
    release.set()
    await consumer_task


async def test_producer_excludes_messages_at_or_before_review_cursor(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.last_reviewed_message_id = "555"
    for message_id in (554, 555, 556):
        state.buffer.append(
            proactive.channel_message_from_hikari(_hikari_message(id=message_id))
        )

    await proactive._run_producer(state)

    context = wake_setup.producers[0].contexts[0]
    assert [message.id for message in context.new_messages] == ["556"]
    assert state.last_reviewed_message_id == "556"


async def test_producer_of_only_reviewed_messages_is_a_noop(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.last_reviewed_message_id = "555"
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await proactive._run_producer(state)

    assert wake_setup.producers == []
    assert state.buffer == []


async def test_drain_notifications_renders_new_arrivals_mid_wake(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(_hikari_message(id=555))
        )
    )
    drained: list[str] = []
    original_consume = wake_setup.consumer.consume

    async def consume_reading_notifications(context):
        arrived = proactive.channel_message_from_hikari(
            _hikari_message(id=777, content="did you see this?")
        )
        state.queue.push(proactive.mention_notification(arrived))
        deps = wake_setup.captured_kwargs[-1]["deps_factory"](
            env=None,
            actions=None,
            instruction_store=None,
            skim_transcript=None,
            budget=None,
        )
        drained.append(deps.drain_notifications())
        drained.append(deps.drain_notifications())
        return await original_consume(context)

    wake_setup.consumer.consume = consume_reading_notifications
    await proactive._consume_channel_once(state)

    assert "did you see this?" in drained[0]
    assert drained[1] == "No new notifications."


# --- memory injection --------------------------------------------------------


def test_render_memory_block_composes_known_sections():
    note = SimpleNamespace(
        channel_name="#general", channel_id="1", text="alice is benchmarking her parser"
    )
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
    assert (
        proactive.render_memory_block(
            long_term_memory=None,
            long_term_updated_at=None,
            notes=(),
            topic=None,
            channel_notes=None,
        )
        == ""
    )


async def test_wake_injects_memory_hourly_not_per_wake(wake_setup, monkeypatch):
    captured_kwargs = []
    consumer = wake_setup.consumer

    def capturing_factory(**kwargs):
        captured_kwargs.append(kwargs)
        consumer.queue = kwargs["notification_queue"]
        return consumer

    monkeypatch.setattr(proactive, "AgentConsumer", capturing_factory)

    async def fake_memory(run, state):
        return "YOUR MEMORY: the guild loves rust"

    monkeypatch.setattr(proactive, "load_memory_block", fake_memory)

    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))
    await _produce_and_consume(state)
    assert captured_kwargs[-1]["brief_preamble"].startswith("YOUR MEMORY")

    # A second wake inside the hour carries no memory block.
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=556)))
    await _produce_and_consume(state)
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
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=556)))
    await _produce_and_consume(state)
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
        _hikari_message(
            id=502,
            created_at=fresh,
            author=SimpleNamespace(
                id=999, username="smarter-bot", global_name=None, is_bot=True
            ),
        ),  # bot-authored: excluded from catch-up
        _hikari_message(
            id=499, created_at=fresh - timedelta(hours=2)
        ),  # older than the catch-up age cap: excluded
    ]
    persistence_setup.bot.rest.fetch_messages = (
        lambda channel_id, after=None: _FakeIterator(missed)
    )
    produced = []

    async def fake_run_producer(state, passive=False):
        produced.append(
            (
                [m.id for m in state.buffer],
                [notification.kind for notification in state.queue.items],
                passive,
            )
        )
        state.buffer.clear()

    monkeypatch.setattr(proactive, "_run_producer", fake_run_producer)
    await proactive._recover_channels(persistence_setup.runtime)
    assert produced == [(["501"], ["recovery"], True)]


async def test_recovery_queues_and_consumes_missed_mention(
    persistence_setup, monkeypatch
):
    await persistence_setup.store.write_cursor(1, guild_id="2", last_message_id="500")
    missed_mention = _hikari_message(
        id=501,
        created_at=datetime.now(UTC),
        user_mentions_ids=(999,),
        content="@smarter-bot can you help?",
    )
    persistence_setup.bot.rest.fetch_messages = (
        lambda channel_id, after=None: _FakeIterator([missed_mention])
    )
    persistence_setup.runtime._agent_model_id = "stub"
    persistence_setup.runner.history = []
    persistence_setup.runner.wake = AsyncMock(
        return_value=(
            "handled missed mention",
            {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
        )
    )
    monkeypatch.setattr(proactive, "WatcherProducer", WatcherProducer)
    monkeypatch.setattr(proactive, "AgentConsumer", AgentConsumer)
    monkeypatch.setattr(proactive, "load_memory_block", AsyncMock(return_value=""))

    await proactive._recover_channels(persistence_setup.runtime)

    state = persistence_setup.runtime.states[1]
    engagement = next(
        notification
        for notification in state.queue.items
        if notification.kind == "mention"
    )
    assert engagement.message_ids == ("501",)

    await proactive._consumer_loop_iteration(state)

    persistence_setup.runner.wake.assert_awaited_once()
    brief = persistence_setup.runner.wake.await_args.args[0]
    assert "You were @mentioned" in brief
    assert "@smarter-bot can you help?" in brief
    assert state.queue.items == []


async def test_recovery_skips_disabled_channels(persistence_setup, monkeypatch):
    await persistence_setup.store.write_cursor(1, guild_id="2", last_message_id="500")
    persistence_setup.service.settings = ProactiveChannelSettings(
        guild_id="2",
        channel_id="1",
        enabled=False,
        watch_addendum="",
    )
    produced = []

    async def fake_run_producer(state, passive=False):
        produced.append(state)

    monkeypatch.setattr(proactive, "_run_producer", fake_run_producer)
    await proactive._recover_channels(persistence_setup.runtime)
    assert produced == []


async def test_history_store_round_trips_and_survives_garbage():
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.messages import UserPromptPart

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
    monkeypatch.setattr(wake_setup.runtime, "history_store", lambda: store)
    runner = SimpleNamespace(history=["fresh"])
    monkeypatch.setattr(wake_setup.runtime, "agent_runner_for", lambda state: runner)
    wake_setup.store = store
    wake_setup.runner = runner
    return wake_setup


async def test_wake_loads_and_persists_agent_history(persistence_setup):
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.messages import UserPromptPart

    stored = [ModelRequest(parts=[UserPromptPart("earlier wake")])]
    await persistence_setup.store.write(1, stored)

    state = persistence_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    async def activate_leaving_history(context):
        # The runner would normally append the turn; simulate that.
        persistence_setup.runner.history = list(persistence_setup.runner.history) + [
            ModelRequest(parts=[UserPromptPart("this wake")])
        ]
        return persistence_setup.consumer.result

    persistence_setup.consumer.consume = activate_leaving_history
    await _produce_and_consume(state)

    # Stored history was loaded into the runner before the wake…
    assert "earlier wake" in str(persistence_setup.runner.history[0])
    # …and the post-wake history (with the new turn) was persisted.
    persisted = await persistence_setup.store.read(1)
    assert any("this wake" in str(m) for m in persisted)
    assert state.history_loaded is True


async def test_wake_posts_generated_images(persistence_setup):
    from smarter_dev.bot.agents.chat_tools import GeneratedImage

    state = persistence_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))
    captured_factory = {}

    original_factory = proactive.AgentConsumer

    def factory_with_images(**kwargs):
        captured_factory["deps_factory"] = kwargs["deps_factory"]
        persistence_setup.consumer.queue = kwargs["notification_queue"]
        return persistence_setup.consumer

    proactive.AgentConsumer = factory_with_images
    try:

        async def activate_generating_image(context):
            deps = captured_factory["deps_factory"](
                env=None,
                actions=None,
                instruction_store=None,
                skim_transcript=None,
                budget=None,
            )
            deps.pending_images.append(
                GeneratedImage(data=b"png", mime_type="image/png", filename="art.png")
            )
            return persistence_setup.consumer.result

        persistence_setup.consumer.consume = activate_generating_image
        await _produce_and_consume(state)
    finally:
        proactive.AgentConsumer = original_factory

    calls = persistence_setup.bot.rest.create_message.await_args_list
    # First the text reply, then the image message with attachments.
    assert len(calls) == 2
    image_kwargs = calls[1].kwargs
    assert len(image_kwargs["attachments"]) == 1
    assert image_kwargs["reply"] == 555


# --- notification consumer loop --------------------------------------------


async def test_consumer_iteration_runs_when_waking_notification_arrives(
    monkeypatch,
):
    state = proactive.ChannelWatchState(guild_id="2", channel_id="1")
    consumed = []

    async def fake_consume(target_state):
        consumed.append(target_state)

    monkeypatch.setattr(proactive, "_consume_channel_once", fake_consume)
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(
                _hikari_message(user_mentions_ids=(999,))
            )
        )
    )

    await proactive._consumer_loop_iteration(state)

    assert consumed == [state]


async def test_waking_notification_mid_wake_causes_immediate_followup(
    monkeypatch,
):
    state = proactive.ChannelWatchState(guild_id="2", channel_id="1")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_finished = asyncio.Event()
    calls = 0

    async def fake_consume(target_state):
        nonlocal calls
        calls += 1
        target_state.queue.drain()
        if calls == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_finished.set()

    monkeypatch.setattr(proactive, "_consume_channel_once", fake_consume)
    consumer = asyncio.create_task(proactive._consumer_loop(state))
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(
                _hikari_message(id=555, user_mentions_ids=(999,))
            )
        )
    )
    await first_started.wait()
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(
                _hikari_message(id=556, user_mentions_ids=(999,))
            )
        )
    )
    release_first.set()
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert calls == 2


async def test_passive_non_waking_producer_result_never_runs_agent(monkeypatch):
    state = proactive.ChannelWatchState(guild_id="2", channel_id="1")
    consumed = []

    async def fake_consume(target_state):
        consumed.append(target_state)

    monkeypatch.setattr(proactive, "_consume_channel_once", fake_consume)
    state.queue.push(
        watcher_summary_notification(
            summary="ordinary chatter",
            message_ids=["555"],
            wake=False,
            created_at=datetime.now(UTC),
        )
    )
    iteration = asyncio.create_task(proactive._consumer_loop_iteration(state))
    await asyncio.sleep(0)

    assert not iteration.done()
    assert consumed == []
    iteration.cancel()
    with pytest.raises(asyncio.CancelledError):
        await iteration
