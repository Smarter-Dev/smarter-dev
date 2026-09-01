"""Unit tests for the notification-producing two-pass adapter halves."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from smarter_dev.bot.proactive import adapter
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.notifications import NotificationQueue
from smarter_dev.bot.proactive.notifications import mode_change_notification
from smarter_dev.bot.proactive.types import ActivationContext
from smarter_dev.bot.proactive.types import ChannelMessage
from smarter_dev.bot.proactive.watcher import WatcherDecision

T = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _message(
    message_id: str,
    offset: int,
    *,
    author_id: str = "user-1",
    mentions: tuple[str, ...] = (),
) -> ChannelMessage:
    return ChannelMessage(
        id=message_id,
        timestamp=T + timedelta(seconds=offset),
        author_id=author_id,
        author_name="alice",
        author_display="Alice",
        is_bot=False,
        content=f"message {message_id}",
        reply_to_id=None,
        mention_user_ids=mentions,
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


def _context(
    *new_messages: ChannelMessage, channel_id: str = ""
) -> ActivationContext:
    return ActivationContext(
        channel_name="general",
        guild_name="Smarter Dev",
        bot_user_id="bot-1",
        activated_at=T + timedelta(minutes=1),
        history=[_message("history", 0)],
        new_messages=list(new_messages) or [_message("new", 1)],
        channel_id=channel_id,
    )


class _ExplodingWatcher:
    async def decide(self, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("watcher must not be consulted on engagement")


class _StubWatcher:
    def __init__(self, decision: WatcherDecision):
        self.decision = decision

    async def decide(self, **kwargs):
        return self.decision, {
            "input_tokens": 11,
            "output_tokens": 2,
            "cache_read_tokens": 3,
        }


def _producer(watcher, queue: NotificationQueue) -> adapter.WatcherProducer:
    return adapter.WatcherProducer(
        watcher=watcher,
        instruction_store=InstructionStore(seed="SEED"),
        watcher_model_id="watcher-model",
        notification_queue=queue,
    )


async def test_watcher_producer_pushes_engagement_without_watcher_call():
    queue = NotificationQueue()
    producer = _producer(_ExplodingWatcher(), queue)

    usage = await producer.activate(
        _context(
            _message("mention", 2, mentions=("bot-1",)),
            channel_id="123",
        )
    )

    assert usage == {}
    assert len(queue.items) == 1
    assert queue.items[0].kind == "mention"
    assert queue.items[0].channel_id == "123"
    assert queue.items[0].channel_name == "general"
    assert queue.items[0].message_ids == ("mention",)
    assert queue.items[0].wakes is True


async def test_watcher_producer_pushes_waking_watcher_summary():
    queue = NotificationQueue()
    producer = _producer(
        _StubWatcher(
            WatcherDecision(
                wake=True,
                reason="open question",
                relevant_message_ids=["new"],
                summary="Alice asked the room",
            )
        ),
        queue,
    )

    await producer.activate(_context(channel_id="123"))

    assert len(queue.items) == 1
    assert queue.items[0].kind == "watcher_summary"
    assert queue.items[0].channel_id == "123"
    assert queue.items[0].channel_name == "general"
    assert queue.items[0].message_ids == ("new",)
    assert queue.items[0].wakes is True


async def test_watcher_producer_discards_non_waking_summary():
    queue = NotificationQueue()
    producer = _producer(
        _StubWatcher(WatcherDecision(wake=False, reason="ordinary chat")),
        queue,
    )

    await producer.activate(_context())

    assert queue.items == []


async def test_watcher_producer_returns_only_watcher_usage():
    queue = NotificationQueue()
    producer = _producer(
        _StubWatcher(WatcherDecision(wake=False, reason="ordinary chat")),
        queue,
    )

    usage = await producer.activate(_context())

    assert usage == {
        "watcher-model": {
            "input_tokens": 11,
            "output_tokens": 2,
            "cache_read_tokens": 3,
        }
    }
    assert "agent-model" not in usage


class _RecordingAgentRunner:
    def __init__(self):
        self.briefs: list[str] = []
        self.deps = []

    async def wake(self, brief, deps):
        self.briefs.append(brief)
        self.deps.append(deps)
        return "observed", {
            "input_tokens": 21,
            "output_tokens": 4,
            "cache_read_tokens": 1,
        }


class _RecordingDepsFactory:
    def __init__(self):
        self.kwargs: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        return adapter.AgentDeps(**kwargs)


class _StubSkimRunner:
    async def skim(self, transcript):
        return "skimmed transcript", {
            "input_tokens": 8,
            "output_tokens": 3,
            "cache_read_tokens": 2,
        }


class _SkimmingAgentRunner:
    async def wake(self, brief, deps):
        skimmed = await deps.skim_transcript("long transcript")
        assert skimmed == "skimmed transcript"
        return "observed", {
            "input_tokens": 21,
            "output_tokens": 4,
            "cache_read_tokens": 1,
        }


async def test_agent_consumer_drains_exact_notifications_and_returns_agent_usage(
    monkeypatch,
):
    queue = NotificationQueue()
    first = mode_change_notification(
        mode="active", cause="requested", until=None, created_at=T,
        channel_id="1", channel_name="general",
    )
    second = mode_change_notification(
        mode="passive",
        cause="quiet",
        until=None,
        created_at=T + timedelta(seconds=1),
        channel_id="1",
        channel_name="general",
    )
    queue.push(first)
    queue.push(second)
    built_with = []

    def recording_build_wake_brief(
        notifications, dropped, instruction_stores, enabled_channels
    ):
        built_with.append(
            (notifications, dropped, instruction_stores, enabled_channels)
        )
        return "rendered brief"

    monkeypatch.setattr(adapter, "build_wake_brief", recording_build_wake_brief)
    runner = _RecordingAgentRunner()
    deps_factory = _RecordingDepsFactory()
    store = InstructionStore(seed="SEED")
    consumer = adapter.AgentConsumer(
        agent_runner=runner,
        skim=None,
        instruction_stores={"general": store},
        enabled_channels={"general": "general"},
        agent_model_id="agent-model",
        notification_queue=queue,
        deps_factory=deps_factory,
        brief_preamble="MEMORY REFRESH",
    )

    result = await consumer.activate(_context())

    assert built_with == [
        ([first, second], 0, {"general": store}, {"general": "general"})
    ]
    assert runner.briefs == ["MEMORY REFRESH\n\nrendered brief"]
    assert len(deps_factory.kwargs) == 1
    assert runner.deps == [adapter.AgentDeps(**deps_factory.kwargs[0])]
    assert queue.items == []
    assert result.usage_by_model == {
        "agent-model": {
            "input_tokens": 21,
            "output_tokens": 4,
            "cache_read_tokens": 1,
        }
    }
    assert "watcher-model" not in result.usage_by_model


async def test_agent_consumer_returns_skim_usage_attributed_to_watcher_model():
    consumer = adapter.AgentConsumer(
        agent_runner=_SkimmingAgentRunner(),
        skim=_StubSkimRunner(),
        instruction_stores={"general": InstructionStore(seed="SEED")},
        enabled_channels={"general": "general"},
        agent_model_id="agent-model",
        notification_queue=NotificationQueue(),
        watcher_model_id="watcher-model",
    )

    result = await consumer.activate(_context())

    assert result.usage_by_model == {
        "watcher-model": {
            "input_tokens": 8,
            "output_tokens": 3,
            "cache_read_tokens": 2,
        },
        "agent-model": {
            "input_tokens": 21,
            "output_tokens": 4,
            "cache_read_tokens": 1,
        },
    }
    assert result.input_tokens == 29
    assert result.output_tokens == 7
    assert result.cache_read_tokens == 3


async def test_agent_consumer_rejects_skim_usage_without_watcher_model_id():
    consumer = adapter.AgentConsumer(
        agent_runner=_SkimmingAgentRunner(),
        skim=_StubSkimRunner(),
        instruction_stores={"general": InstructionStore(seed="SEED")},
        enabled_channels={"general": "general"},
        agent_model_id="agent-model",
        notification_queue=NotificationQueue(),
    )

    try:
        await consumer.consume(_context())
    except ValueError as error:
        assert str(error) == "watcher_model_id is required to attribute skim usage"
    else:
        raise AssertionError("skim usage without watcher_model_id must fail")
