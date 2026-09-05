"""Tests for the proactive plugin: debounce math, conversion, wake flow."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.agents.chat_tools import GeneratedImage
from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.adapter import AgentConsumer
from smarter_dev.bot.proactive.adapter import WatcherProducer
from smarter_dev.bot.proactive.contracts import ControlCommand
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.proactive.notifications import watcher_summary_notification
from smarter_dev.bot.proactive.types import ActivationResult
from smarter_dev.bot.proactive.types import ProposedReaction
from smarter_dev.bot.proactive.types import ProposedResponse
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.proactive_settings_service import EnabledProactiveChannel
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


def test_execution_destination_can_be_selected_per_guild(monkeypatch):
    monkeypatch.setenv(proactive.EXTERNAL_GUILDS_ENV_VAR, "2, 3")
    monkeypatch.setenv(proactive.SHADOW_GUILDS_ENV_VAR, "4")
    run = proactive.ProactiveRuntime(
        SimpleNamespace(d={}), start_consumers=False
    )

    assert run.execution_mode_for("1") == proactive.EMBEDDED_EXECUTION_MODE
    assert run.execution_mode_for("2") == proactive.EXTERNAL_EXECUTION_MODE
    assert run.execution_mode_for("4") == proactive.SHADOW_EXECUTION_MODE


def test_embedded_override_supports_single_guild_rollback(monkeypatch):
    monkeypatch.setenv(proactive.EMBEDDED_GUILDS_ENV_VAR, "2")
    run = proactive.ProactiveRuntime(
        SimpleNamespace(d={}),
        start_consumers=False,
        execution_mode=proactive.EXTERNAL_EXECUTION_MODE,
    )

    assert run.execution_mode_for("1") == proactive.EXTERNAL_EXECUTION_MODE
    assert run.execution_mode_for("2") == proactive.EMBEDDED_EXECUTION_MODE


async def test_worker_control_command_changes_bot_owned_active_window():
    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda _channel_id: SimpleNamespace(name="general")
        ),
    )
    run = proactive.ProactiveRuntime(bot, start_consumers=False)
    store = SimpleNamespace(write_active_until=AsyncMock())
    run.history_store = lambda: store
    run.enqueue_notification = AsyncMock()
    command = ControlCommand(
        command_id=uuid4(),
        guild_id="2",
        channel_id="1",
        mode="active",
        minutes=7,
        created_at=datetime.now(UTC),
        trace_id=uuid4(),
    )

    await proactive._apply_control_command(run, command)

    assert run.state_for(2, 1).active_until > proactive.time.monotonic()
    assert store.write_active_until.await_args.kwargs["ttl_seconds"] == 420
    queued = run.enqueue_notification.await_args.args[1]
    assert queued.kind == "mode_change"


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
                channel_id="1",
                channel_name=context.channel_name,
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
    def __init__(
        self,
        enabled: bool = True,
        addendum: str = "",
        enabled_rows: list[EnabledProactiveChannel] | None = None,
    ):
        self.settings = ProactiveChannelSettings(
            guild_id="2",
            channel_id="1",
            enabled=enabled,
            watch_addendum=addendum,
        )
        self.saved_addenda: list[str] = []
        self.recorded_usage: list[dict] = []
        self.usage_error: Exception | None = None
        self.enabled_rows = enabled_rows
        self.list_error: Exception | None = None

    async def get_settings(self, guild_id, channel_id):
        return self.settings

    async def list_enabled_channels(self, guild_id):
        if self.list_error is not None:
            raise self.list_error
        if self.enabled_rows is not None:
            return self.enabled_rows
        if not self.settings.enabled:
            return []
        return [
            EnabledProactiveChannel(
                channel_id=self.settings.channel_id,
                watch_addendum=self.settings.watch_addendum,
            )
        ]

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


def _fake_bot(history_messages, service, *, channel_names=None):
    channel_names = channel_names or {1: "general"}

    def fetch_messages(channel_id):
        if isinstance(history_messages, dict):
            channel_history = history_messages.get(channel_id, [])
            if isinstance(channel_history, Exception):
                raise channel_history
            return _FakeIterator(channel_history)
        return _FakeIterator(history_messages)

    rest = SimpleNamespace(
        fetch_messages=fetch_messages,
        create_message=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    return SimpleNamespace(
        rest=rest,
        d={"proactive_settings_service": service},
        get_me=lambda: SimpleNamespace(id=999, username="smarter-bot"),
        cache=SimpleNamespace(
            get_guild_channel=lambda cid: SimpleNamespace(
                name=channel_names.get(cid, str(cid))
            ),
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
        responses=[
            ProposedResponse(reply_to_id="555", content="happy to help", channel_id="1")
        ],
        input_tokens=10,
        output_tokens=1,
        cache_read_tokens=0,
        model_id="stub",
        reactions=(ProposedReaction(message_id="555", emoji="👍", channel_id="1"),),
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
        captured_stores.extend(kwargs["instruction_stores"].values())
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
    guild_state = proactive._runtime().guild_state_for(int(state.guild_id))
    if guild_state.queue.items:
        await proactive._consume_guild_once(guild_state)


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


async def test_producer_context_carries_channel_provenance(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await proactive._run_producer(state)

    context = wake_setup.producers[0].contexts[0]
    assert context.channel_id == "1"
    assert context.channel_name == "general"


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


async def test_wake_persists_only_the_channel_whose_instructions_changed(
    wake_setup,
):
    wake_setup.service.enabled_rows = [
        EnabledProactiveChannel(channel_id="1", watch_addendum="watch one"),
        EnabledProactiveChannel(channel_id="2", watch_addendum="watch two"),
    ]
    save_addendum = AsyncMock(return_value=wake_setup.service.settings)
    wake_setup.service.set_watch_addendum = save_addendum
    guild_state = wake_setup.runtime.guild_state_for(2)
    guild_state.queue.push(
        watcher_summary_notification(
            summary="wake",
            message_ids=["202"],
            wake=True,
            created_at=datetime.now(UTC),
            channel_id="2",
            channel_name="benchmarks",
        )
    )

    async def update_second_channel(context):
        wake_setup.captured_stores[1].set_instruction("watch the benchmark")
        guild_state.queue.drain()
        return wake_setup.consumer.result

    wake_setup.consumer.consume = update_second_channel

    await proactive._consume_guild_once(guild_state)

    save_addendum.assert_awaited_once()
    assert save_addendum.await_args.args[:2] == ("2", "2")
    assert "watch the benchmark" in save_addendum.await_args.args[2]


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
    await proactive._consume_guild_once(wake_setup.runtime.guild_state_for(2))

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
        "responses": [
            ProposedResponse(reply_to_id="555", content="happy to help", channel_id="1")
        ],
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
    assert agent_record["channel_id"] == "guild-wide"
    assert agent_record["responses"] == 1
    agent = agent_record["entries"][0]
    assert agent["operation"] == "agent"
    assert agent["input_tokens"] == 200


async def test_agent_usage_counts_responses_not_discord_message_parts(wake_setup):
    wake_setup.consumer.result = _usage_result(
        responses=[
            ProposedResponse(
                reply_to_id="555",
                content="x" * 2500,
                channel_id="1",
            )
        ],
        usage_by_model={
            "gemini-3.7-flash": {
                "input_tokens": 200,
                "output_tokens": 10,
                "cache_read_tokens": 0,
            }
        },
    )
    state = wake_setup.runtime.state_for(2, 1)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await _produce_and_consume(state)

    assert wake_setup.bot.rest.create_message.await_count == 2
    assert wake_setup.service.recorded_usage[-1]["responses"] == 1


async def test_cross_channel_wake_dispatches_each_action_to_its_channel(wake_setup):
    wake_setup.service.enabled_rows = [
        EnabledProactiveChannel(channel_id="1", watch_addendum="watch one"),
        EnabledProactiveChannel(channel_id="2", watch_addendum="watch two"),
    ]
    wake_setup.bot.cache.get_guild_channel = lambda channel_id: SimpleNamespace(
        name={1: "general", 2: "benchmarks"}[channel_id]
    )
    guild_state = wake_setup.runtime.guild_state_for(2)
    for channel_id, channel_name, summary in (
        ("1", "general", "first channel activity"),
        ("2", "benchmarks", "second channel activity"),
    ):
        guild_state.queue.push(
            watcher_summary_notification(
                summary=summary,
                message_ids=[f"10{channel_id}"],
                wake=True,
                created_at=datetime.now(UTC),
                channel_id=channel_id,
                channel_name=channel_name,
            )
        )
    wake_setup.consumer.result = ActivationResult(
        responses=[
            ProposedResponse(reply_to_id="101", content="reply one", channel_id="1"),
            ProposedResponse(reply_to_id="202", content="reply two", channel_id="2"),
        ],
        reactions=(
            ProposedReaction(message_id="101", emoji="1️⃣", channel_id="1"),
            ProposedReaction(message_id="202", emoji="2️⃣", channel_id="2"),
        ),
        input_tokens=10,
        output_tokens=2,
        cache_read_tokens=0,
        model_id="stub",
    )
    briefs = []

    async def consume_both_channels(context):
        items, dropped = guild_state.queue.drain()
        briefs.append(proactive.render_notifications(items, dropped))
        deps = wake_setup.captured_kwargs[-1]["deps_factory"](
            env=None,
            actions=None,
            instruction_store=None,
            skim_transcript=None,
            budget=None,
        )
        deps.pending_images.extend(
            [
                GeneratedImage(
                    data=b"one",
                    mime_type="image/png",
                    filename="one.png",
                    channel_id="1",
                ),
                GeneratedImage(
                    data=b"two",
                    mime_type="image/png",
                    filename="two.png",
                    channel_id="2",
                ),
            ]
        )
        return wake_setup.consumer.result

    wake_setup.consumer.consume = consume_both_channels

    await proactive._consume_guild_once(guild_state)

    assert "first channel activity" in briefs[0]
    assert "second channel activity" in briefs[0]
    calls = wake_setup.bot.rest.create_message.await_args_list
    assert calls[0].args == (1, "reply one")
    assert calls[1].args == (2, "reply two")
    assert calls[2].args == (1,)
    assert calls[2].kwargs["reply"] == 101
    assert calls[3].args == (2,)
    assert calls[3].kwargs["reply"] == 202
    assert wake_setup.bot.rest.add_reaction.await_args_list[0].args == (1, 101, "1️⃣")
    assert wake_setup.bot.rest.add_reaction.await_args_list[1].args == (2, 202, "2️⃣")
    assert wake_setup.runtime.channel_states == {}


async def test_consumer_rejects_response_for_non_enabled_channel(wake_setup):
    guild_state = wake_setup.runtime.guild_state_for(2)
    guild_state.queue.push(
        watcher_summary_notification(
            summary="wake",
            message_ids=["101"],
            wake=True,
            created_at=datetime.now(UTC),
            channel_id="1",
            channel_name="general",
        )
    )
    wake_setup.consumer.result = ActivationResult(
        responses=[
            ProposedResponse(reply_to_id="909", content="must not send", channel_id="9")
        ],
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        model_id="stub",
    )

    await proactive._consume_guild_once(guild_state)

    wake_setup.bot.rest.create_message.assert_not_awaited()


async def test_unreadable_channel_environment_does_not_abort_guild_wake(wake_setup):
    wake_setup.service.enabled_rows = [
        EnabledProactiveChannel(channel_id="1", watch_addendum=""),
        EnabledProactiveChannel(channel_id="2", watch_addendum=""),
    ]
    wake_setup.bot.rest.fetch_messages = lambda channel_id: (
        (_ for _ in ()).throw(proactive.hikari.HikariError("forbidden"))
        if channel_id == 1
        else _FakeIterator([_hikari_message(id=202)])
    )
    guild_state = wake_setup.runtime.guild_state_for(2)
    guild_state.queue.push(
        watcher_summary_notification(
            summary="wake",
            message_ids=["202"],
            wake=True,
            created_at=datetime.now(UTC),
            channel_id="2",
            channel_name="2",
        )
    )
    wake_setup.consumer.result = ActivationResult(
        responses=[
            ProposedResponse(reply_to_id="202", content="still works", channel_id="2")
        ],
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        model_id="stub",
    )

    async def consume_with_environments(context):
        channel_envs = wake_setup.captured_kwargs[-1]["channel_envs"]
        broken = await channel_envs("1")
        working = await channel_envs("2")
        guild_state.queue.drain()
        assert broken.visible == []
        assert [message.id for message in working.visible] == ["202"]
        return wake_setup.consumer.result

    wake_setup.consumer.consume = consume_with_environments

    await proactive._consume_guild_once(guild_state)

    wake_setup.bot.rest.create_message.assert_awaited_once_with(
        2, "still works", reply=202
    )


async def test_settings_failure_preserves_queue_and_backs_off(wake_setup, monkeypatch):
    wake_setup.service.list_error = APIError("settings unavailable")
    guild_state = wake_setup.runtime.guild_state_for(2)
    guild_state.queue.push(
        watcher_summary_notification(
            summary="keep me",
            message_ids=["101"],
            wake=True,
            created_at=datetime.now(UTC),
            channel_id="1",
            channel_name="general",
        )
    )
    queued_before = list(guild_state.queue.items)
    sleep = AsyncMock()
    monkeypatch.setattr(proactive.asyncio, "sleep", sleep)

    await proactive._consume_guild_once(guild_state)

    assert guild_state.queue.items == queued_before
    sleep.assert_awaited_once()


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


# --- status command ---------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "active", "expected_status", "expected_mode"),
    [
        (True, True, "on", "active"),
        (True, False, "on", "passive"),
        (False, True, "off", "passive"),
    ],
)
async def test_status_reports_channel_enabled_state_and_monitoring_mode(
    wake_setup,
    monkeypatch,
    enabled,
    active,
    expected_status,
    expected_mode,
):
    wake_setup.service.settings = ProactiveChannelSettings(
        guild_id="2",
        channel_id="1",
        enabled=enabled,
        watch_addendum="watch deployments" if enabled else "",
    )
    if active:
        state = wake_setup.runtime.state_for(2, 1)
        state.active_until = proactive.time.monotonic() + 60
    monkeypatch.setattr(
        proactive,
        "deny_without_moderator_permissions",
        AsyncMock(return_value=False),
    )
    context = SimpleNamespace(
        guild_id=2,
        channel_id=1,
        respond=AsyncMock(),
    )

    await proactive.proactive_status(context)

    response = context.respond.await_args.args[0]
    assert f"Proactive bot: **{expected_status}**" in response
    assert f"(monitoring: {expected_mode})" in response
    if enabled:
        assert "watch deployments" in response
    else:
        assert "Watch instructions:\n(none set)" in response


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
    state = listener_setup.runtime.channel_states[1]
    assert len(state.buffer) == 1
    assert listener_setup.scheduled == []  # waits for the 15-min sweep


async def test_engagement_flips_to_active_and_arms_the_debounce(listener_setup):
    await proactive.on_guild_message(_event(_hikari_message(user_mentions_ids=(999,))))
    state = listener_setup.runtime.channel_states[1]
    assert state.active_until > proactive.time.monotonic()
    assert listener_setup.scheduled == [state]

    # Ordinary chatter during the active window also ingests fast.
    await proactive.on_guild_message(_event(_hikari_message(id=556)))
    assert listener_setup.scheduled == [state, state]


async def test_passive_sweep_drains_buffered_channels(listener_setup):
    await proactive.on_guild_message(_event(_hikari_message()))
    state = listener_setup.runtime.channel_states[1]
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


async def test_passive_ticker_sweeps_after_two_minutes_then_every_fifteen(monkeypatch):
    run = SimpleNamespace()
    delays = []
    sweep = AsyncMock()

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(proactive, "runtime", run)
    monkeypatch.setattr(proactive.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(proactive, "_passive_sweep", sweep)

    with pytest.raises(asyncio.CancelledError):
        await proactive._passive_ticker()

    assert delays == [120, 900, 900]
    assert sweep.await_count == 2
    sweep.assert_awaited_with(run)


async def test_passive_ticker_stops_if_runtime_removed_during_initial_wait(monkeypatch):
    sleep = AsyncMock()
    sweep = AsyncMock()
    monkeypatch.setattr(proactive, "runtime", None)
    monkeypatch.setattr(proactive.asyncio, "sleep", sleep)
    monkeypatch.setattr(proactive, "_passive_sweep", sweep)

    await proactive._passive_ticker()

    sleep.assert_awaited_once_with(120)
    sweep.assert_not_awaited()


async def test_mention_queues_verbatim_while_consumer_is_busy(listener_setup):
    state = listener_setup.runtime.state_for(2, 1)

    await proactive.on_guild_message(_event(_hikari_message(user_mentions_ids=(999,))))

    assert listener_setup.scheduled == [state]
    queue = listener_setup.runtime.guild_state_for(2).queue
    mention = next(n for n in queue.items if n.kind == "mention")
    assert mention.channel_id == "1"
    assert mention.channel_name == "general"
    assert "hey there" in mention.body
    assert mention.wakes is True


async def test_plain_active_message_buffers_for_the_producer(listener_setup):
    state = listener_setup.runtime.state_for(2, 1)
    state.active_until = proactive.time.monotonic() + 600

    await proactive.on_guild_message(_event(_hikari_message()))

    assert listener_setup.runtime.guild_state_for(2).queue.items == []
    assert [message.id for message in state.buffer] == ["555"]
    assert listener_setup.scheduled == [state]


async def test_producer_runs_while_agent_consumer_is_mid_wake(wake_setup):
    state = wake_setup.runtime.state_for(2, 1)
    guild_state = wake_setup.runtime.guild_state_for(2)
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))
    guild_state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(_hikari_message(id=554)),
            channel_id="1",
            channel_name="general",
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
    consumer_task = asyncio.create_task(proactive._consume_guild_once(guild_state))
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
    guild_state = wake_setup.runtime.guild_state_for(2)
    guild_state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(_hikari_message(id=555)),
            channel_id="1",
            channel_name="general",
        )
    )
    drained: list[str] = []
    original_consume = wake_setup.consumer.consume

    async def consume_reading_notifications(context):
        assert context.channel_id == ""
        arrived = proactive.channel_message_from_hikari(
            _hikari_message(id=777, content="did you see this?")
        )
        guild_state.queue.push(
            proactive.mention_notification(
                arrived, channel_id="1", channel_name="general"
            )
        )
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
    await proactive._consume_guild_once(guild_state)

    assert "[#general]" in drained[0]
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
    )
    assert "GUILD MEMORY (dreamed 2026-08-17)" in block
    assert "alice is benchmarking" in block


def test_render_memory_block_empty_when_nothing_known():
    assert (
        proactive.render_memory_block(
            long_term_memory=None,
            long_term_updated_at=None,
            notes=(),
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
        self.expiries: dict[str, int | None] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.expiries[key] = ex

    async def delete(self, key):
        self.data.pop(key, None)

    async def scan_iter(self, match=None):
        import fnmatch

        for key in list(self.data):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


async def test_cursor_round_trips_with_guild_id():
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


async def test_cursor_advances_only_after_guild_wake_is_consumed(persistence_setup):
    await persistence_setup.store.write_cursor(
        1, guild_id="2", last_message_id="500"
    )
    state = persistence_setup.runtime.state_for(2, 1)
    state.last_reviewed_message_id = "500"
    state.buffer.append(proactive.channel_message_from_hikari(_hikari_message(id=555)))

    await proactive._run_producer(state)

    cursor_before_wake = await persistence_setup.store.read_cursor(1)
    assert cursor_before_wake == {"guild_id": "2", "last_message_id": "500"}

    await proactive._consume_guild_once(persistence_setup.runtime.guild_state_for(2))

    cursor_after_wake = await persistence_setup.store.read_cursor(1)
    assert cursor_after_wake == {"guild_id": "2", "last_message_id": "555"}


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
        guild_queue = persistence_setup.runtime.guild_state_for(
            int(state.guild_id)
        ).queue
        produced.append(
            (
                [m.id for m in state.buffer],
                [notification.kind for notification in guild_queue.items],
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

    guild_state = persistence_setup.runtime.guild_state_for(2)
    engagement = next(
        notification
        for notification in guild_state.queue.items
        if notification.kind == "mention"
    )
    assert engagement.message_ids == ("501",)

    await proactive._consumer_loop_iteration(guild_state)

    persistence_setup.runner.wake.assert_awaited_once()
    brief = persistence_setup.runner.wake.await_args.args[0]
    assert "You were @mentioned" in brief
    assert "@smarter-bot can you help?" in brief
    assert guild_state.queue.items == []


async def test_recovery_retries_a_transient_settings_failure(
    persistence_setup, monkeypatch
):
    await persistence_setup.store.write_cursor(1, guild_id="2", last_message_id="500")
    missed_mention = _hikari_message(
        id=501,
        created_at=datetime.now(UTC),
        user_mentions_ids=(999,),
        content="@smarter-bot are you back?",
    )
    persistence_setup.bot.rest.fetch_messages = (
        lambda channel_id, after=None: _FakeIterator([missed_mention])
    )
    persistence_setup.service.get_settings = AsyncMock(
        side_effect=[
            APIError("settings temporarily unavailable"),
            persistence_setup.service.settings,
            persistence_setup.service.settings,
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(proactive.asyncio, "sleep", sleep)
    monkeypatch.setattr(proactive, "WatcherProducer", WatcherProducer)

    await proactive._recover_channels(persistence_setup.runtime)

    assert persistence_setup.service.get_settings.await_count == 3
    sleep.assert_awaited_once_with(proactive.SETTINGS_RETRY_BACKOFF_SECONDS)
    guild_queue = persistence_setup.runtime.guild_state_for(2).queue
    assert [item.kind for item in guild_queue.items] == ["recovery", "mention"]
    assert all(item.channel_id == "1" for item in guild_queue.items)


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
    store = ProactiveHistoryStore(_FakeRedis())
    assert await store.read(1) == []

    history = [ModelRequest(parts=[UserPromptPart("wake one")])]
    await store.write(1, history)
    loaded = await store.read(1)
    assert "wake one" in str(loaded[0])

    store._redis.data[ProactiveHistoryStore._history_key(1)] = b"not json"
    assert await store.read(1) == []


async def test_guild_history_store_uses_a_distinct_key_and_survives_garbage():
    redis = _FakeRedis()
    store = ProactiveHistoryStore(redis)
    history = [ModelRequest(parts=[UserPromptPart("guild wake")])]

    await store.write_guild(2, history)

    assert "guild wake" in str((await store.read_guild(2))[0])


def _compaction_runtime(monkeypatch) -> proactive.ProactiveRuntime:
    runtime = proactive.ProactiveRuntime(
        SimpleNamespace(
            cache=SimpleNamespace(get_guild=lambda _gid: None),
            get_me=lambda: None,
        ),
        start_consumers=False,
    )
    runtime._agent_model_id = "agent-model"
    monkeypatch.setattr(proactive, "COMPACTION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(proactive, "build_twopass_model", lambda model_id: model_id)
    monkeypatch.setattr(
        proactive,
        "build_proactive_agent",
        lambda model, system_prompt: SimpleNamespace(),
    )
    return runtime


_COMPACTION_MESSAGES = [ModelRequest(parts=[UserPromptPart("old wake")])]


async def test_compaction_is_written_by_the_agents_own_model(monkeypatch):
    runtime = _compaction_runtime(monkeypatch)
    seen = {}

    async def fake_self_summary(model, messages):
        seen["model"] = model
        seen["messages"] = messages
        return "my own memory note", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(proactive, "self_compaction_summary", fake_self_summary)
    runner = runtime.agent_runner_for(proactive.GuildAgentState(guild_id="2"))

    assert await runner.summarize(_COMPACTION_MESSAGES) == "my own memory note"
    assert seen["model"] == "agent-model"
    assert seen["messages"] is _COMPACTION_MESSAGES


async def test_compaction_times_out_and_falls_back_to_skim(monkeypatch):
    # A hung summarize once blocked the guild consumer loop; the closure
    # must bound the self-summary, fall back to a watcher-model skim, and
    # finally compact by truncation instead of hanging.
    runtime = _compaction_runtime(monkeypatch)

    async def hanging_self_summary(model, messages):
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        proactive, "self_compaction_summary", hanging_self_summary
    )

    class _FallbackSkim:
        async def skim(self, text):
            return "skim summary", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(runtime, "skim", lambda: _FallbackSkim())
    runner = runtime.agent_runner_for(proactive.GuildAgentState(guild_id="2"))
    assert await runner.summarize(_COMPACTION_MESSAGES) == "skim summary"

    class _HangingSkim:
        async def skim(self, text):
            await asyncio.sleep(3600)

    monkeypatch.setattr(runtime, "skim", lambda: _HangingSkim())
    runner = runtime.agent_runner_for(proactive.GuildAgentState(guild_id="3"))
    assert "could not be summarized" in await runner.summarize(
        _COMPACTION_MESSAGES
    )


async def test_active_window_round_trips_with_ttl():
    redis = _FakeRedis()
    store = ProactiveHistoryStore(redis)
    assert await store.read_active_until(1) is None

    await store.write_active_until(1, until_epoch=1234.5, ttl_seconds=600)

    assert await store.read_active_until(1) == 1234.5
    assert redis.expiries[ProactiveHistoryStore._active_until_key(1)] == 600


async def test_engagement_persists_and_restart_restores_the_active_window(
    listener_setup, monkeypatch
):
    store = ProactiveHistoryStore(_FakeRedis())
    monkeypatch.setattr(
        listener_setup.runtime, "history_store", lambda: store
    )
    await proactive.on_guild_message(
        _event(_hikari_message(user_mentions_ids=(999,)))
    )
    stored = await store.read_active_until(1)
    assert stored is not None and stored > proactive.time.time()

    # A fresh runtime (restart) sharing the store: a PLAIN message must find
    # the window still active and arm the fast debounce.
    restarted = proactive.ProactiveRuntime(
        listener_setup.runtime.bot, start_consumers=False
    )
    monkeypatch.setattr(restarted, "history_store", lambda: store)
    monkeypatch.setattr(proactive, "runtime", restarted)
    scheduled = []
    monkeypatch.setattr(
        proactive, "_schedule_producer", lambda state: scheduled.append(state)
    )

    await proactive.on_guild_message(_event(_hikari_message(id=777)))

    state = restarted.channel_states[1]
    assert state.active_until > proactive.time.monotonic()
    assert scheduled == [state]


async def test_history_writes_never_expire():
    # The rolling context is the agent's extended memory: history keys must
    # persist indefinitely, unlike the TTL'd recovery cursors.
    redis = _FakeRedis()
    store = ProactiveHistoryStore(redis)
    history = [ModelRequest(parts=[UserPromptPart("remember me")])]

    await store.write(1, history)
    await store.write_guild(2, history)
    await store.write_cursor(1, guild_id="2", last_message_id="5")

    assert redis.expiries[ProactiveHistoryStore._history_key(1)] is None
    assert redis.expiries[ProactiveHistoryStore._guild_history_key(2)] is None
    assert redis.expiries[ProactiveHistoryStore._cursor_key(1)] is not None
    assert ProactiveHistoryStore._guild_history_key(2) in redis.data
    assert ProactiveHistoryStore._history_key(2) not in redis.data

    redis.data[ProactiveHistoryStore._guild_history_key(2)] = b"not json"
    assert await store.read_guild(2) == []


@pytest.fixture
def persistence_setup(wake_setup, monkeypatch):
    store = ProactiveHistoryStore(_FakeRedis())
    monkeypatch.setattr(wake_setup.runtime, "history_store", lambda: store)
    runner = SimpleNamespace(history=["fresh"])
    monkeypatch.setattr(wake_setup.runtime, "agent_runner_for", lambda state: runner)
    wake_setup.store = store
    wake_setup.runner = runner
    return wake_setup


async def test_wake_loads_and_persists_agent_history(persistence_setup):
    stored = [ModelRequest(parts=[UserPromptPart("earlier wake")])]
    await persistence_setup.store.write_guild(2, stored)

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
    persisted = await persistence_setup.store.read_guild(2)
    assert any("this wake" in str(m) for m in persisted)
    assert persistence_setup.runtime.guild_state_for(2).history_loaded is True


async def test_wake_posts_generated_images(persistence_setup):
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
                GeneratedImage(
                    data=b"png",
                    mime_type="image/png",
                    filename="art.png",
                    channel_id="1",
                )
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
    state = proactive.GuildAgentState(guild_id="2")
    consumed = []

    async def fake_consume(target_state):
        consumed.append(target_state)

    monkeypatch.setattr(proactive, "_consume_guild_once", fake_consume)
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(
                _hikari_message(user_mentions_ids=(999,))
            ),
            channel_id="1",
            channel_name="general",
        )
    )

    await proactive._consumer_loop_iteration(state)

    assert consumed == [state]


async def test_waking_notification_mid_wake_causes_immediate_followup(
    monkeypatch,
):
    state = proactive.GuildAgentState(guild_id="2")
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

    monkeypatch.setattr(proactive, "_consume_guild_once", fake_consume)
    consumer = asyncio.create_task(proactive._consumer_loop(state))
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(
                _hikari_message(id=555, user_mentions_ids=(999,))
            ),
            channel_id="1",
            channel_name="general",
        )
    )
    await first_started.wait()
    state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(
                _hikari_message(id=556, user_mentions_ids=(999,))
            ),
            channel_id="1",
            channel_name="general",
        )
    )
    release_first.set()
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert calls == 2


async def test_passive_non_waking_producer_result_never_runs_agent(monkeypatch):
    state = proactive.GuildAgentState(guild_id="2")
    consumed = []

    async def fake_consume(target_state):
        consumed.append(target_state)

    monkeypatch.setattr(proactive, "_consume_guild_once", fake_consume)
    state.queue.push(
        watcher_summary_notification(
            summary="ordinary chatter",
            message_ids=["555"],
            wake=False,
            created_at=datetime.now(UTC),
            channel_id="1",
            channel_name="general",
        )
    )
    iteration = asyncio.create_task(proactive._consumer_loop_iteration(state))
    await asyncio.sleep(0)

    assert not iteration.done()
    assert consumed == []
    iteration.cancel()
    with pytest.raises(asyncio.CancelledError):
        await iteration


# --- reactions to bot messages ----------------------------------------------


def _reaction_event(*, user_id=42, message_id=900, emoji="👍"):
    return SimpleNamespace(
        guild_id=2,
        channel_id=1,
        message_id=message_id,
        user_id=user_id,
        emoji_name=emoji,
        member=SimpleNamespace(display_name="Dale", username="eviloony"),
    )


def _cached_message(author_id):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id), content="the bot said a thing"
    )


async def test_reaction_on_bot_message_queues_without_waking(wake_setup):
    wake_setup.bot.cache.get_message = lambda _mid: _cached_message(999)

    await proactive.on_guild_reaction(_reaction_event())

    queue = wake_setup.runtime.guild_state_for(2).queue
    assert [n.kind for n in queue.items] == ["reaction"]
    reaction = queue.items[0]
    # Low signal by design: it rides along with the next wake, never
    # waking the agent by itself.
    assert reaction.wakes is False
    assert not queue._wake_event.is_set()
    assert reaction.channel_id == "1"
    assert reaction.message_ids == ("900",)
    assert "LOW signal" in reaction.body
    assert "Dale" in reaction.body and "👍" in reaction.body


async def test_reaction_buffers_for_the_watcher_like_a_message(wake_setup):
    wake_setup.bot.cache.get_message = lambda _mid: _cached_message(999)

    await proactive.on_guild_reaction(_reaction_event())

    state = wake_setup.runtime.channel_states[1]
    assert len(state.buffer) == 1
    buffered = state.buffer[0]
    assert "reacted 👍" in buffered.content and "900" in buffered.content
    assert buffered.author_display == "Dale"
    assert buffered.id.isdigit()  # synthetic snowflake passes the cursor


async def test_reaction_arms_the_debounce_only_in_an_active_window(
    wake_setup, monkeypatch
):
    wake_setup.bot.cache.get_message = lambda _mid: _cached_message(999)
    scheduled = []
    monkeypatch.setattr(
        proactive, "_schedule_producer", lambda state: scheduled.append(state)
    )

    await proactive.on_guild_reaction(_reaction_event())
    assert scheduled == []  # passive channel: waits for the sweep

    state = wake_setup.runtime.channel_states[1]
    state.active_until = proactive.time.monotonic() + 600
    await proactive.on_guild_reaction(_reaction_event(message_id=901))
    assert scheduled == [state]


async def test_reaction_on_someone_elses_message_is_ignored(wake_setup):
    wake_setup.bot.cache.get_message = lambda _mid: _cached_message(42)

    await proactive.on_guild_reaction(_reaction_event())

    assert wake_setup.runtime.guild_state_for(2).queue.items == []


async def test_bots_own_reaction_is_ignored(wake_setup):
    wake_setup.bot.cache.get_message = lambda _mid: _cached_message(999)

    await proactive.on_guild_reaction(_reaction_event(user_id=999))

    assert wake_setup.runtime.guild_state_for(2).queue.items == []


async def test_reaction_in_disabled_channel_is_ignored(wake_setup):
    wake_setup.service.settings = ProactiveChannelSettings(
        guild_id="2", channel_id="1", enabled=False, watch_addendum="",
    )
    wake_setup.bot.cache.get_message = lambda _mid: _cached_message(999)

    await proactive.on_guild_reaction(_reaction_event())

    assert wake_setup.runtime.guild_state_for(2).queue.items == []
