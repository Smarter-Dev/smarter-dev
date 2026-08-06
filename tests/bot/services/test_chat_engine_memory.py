"""Tests for three-layer memory wiring in ``ChannelEngine._run_once``.

The engine owns all of memory's I/O: it loads the guild's blob + today's notes
once per activation, and drains the short-term event log every turn. These tests
exercise the *wiring* — what reaches the rendered prompt on turn one versus turn
two, that the event cursor never delivers the same action twice, that a memory
outage costs the memory and not the turn, and that the writer stage is handed the
identity blob directly instead of having the cheap drafter paraphrase it.

The agents, input builders and budget helpers are patched; Redis is a real
fakeredis so the cursor semantics are the module's own, not a mock's.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

try:
    import fakeredis.aioredis as fakeredis_aioredis
except ImportError:  # pragma: no cover - fakeredis is a dev-only dependency
    fakeredis_aioredis = None

from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_models import BriefingDecision
from smarter_dev.bot.agents.chat_models import ChannelInfo
from smarter_dev.bot.agents.chat_models import FollowupAgentInput
from smarter_dev.bot.agents.chat_models import InitialAgentInput
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import MemoryNote
from smarter_dev.bot.agents.chat_models import Message
from smarter_dev.bot.agents.chat_models import MessageScore
from smarter_dev.bot.agents.chat_models import ResponseBody
from smarter_dev.bot.agents.chat_models import TurnDecision
from smarter_dev.bot.agents.chat_models import WriterBrief
from smarter_dev.bot.agents.chat_models import WriterOutput
from smarter_dev.bot.services.chat_engine import ChannelEngine
from smarter_dev.bot.services.guild_chat_memory_service import EMPTY_SNAPSHOT
from smarter_dev.bot.services.guild_chat_memory_service import GuildMemorySnapshot
from smarter_dev.shared.guild_event_log import append_event
from smarter_dev.shared.guild_event_log import bot_message_event
from smarter_dev.shared.guild_event_log import mod_action_event

pytestmark = pytest.mark.skipif(
    fakeredis_aioredis is None,
    reason="fakeredis is not installed",
)

GUILD_ID = 99
CHANNEL_ID = 42

BLOB = "## Who's here\n- alice (id 1) is deep in shader work and hates cmake."
BLOB_UPDATED_AT = datetime(2026, 8, 6, 0, 20, tzinfo=UTC)


def _snapshot(*, notes: tuple[MemoryNote, ...] = ()) -> GuildMemorySnapshot:
    return GuildMemorySnapshot(
        long_term_memory=BLOB,
        updated_at=BLOB_UPDATED_AT,
        revision=4,
        memory_enabled=True,
        notes=notes,
    )


def _today_note() -> MemoryNote:
    return MemoryNote(
        text="alice (id 1) got soft shadows working and was genuinely giddy about it.",
        channel_id="222",
        channel_name="dev-help",
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )


def _initial_input(**memory) -> InitialAgentInput:
    return InitialAgentInput(
        me=Me(user_id="999", username="bot"),
        channel_history=[Message(message_id="100", author_id="200", body="prior")],
        activation_message=Message(
            message_id="101", author_id="200", body="@bot hi", mentions_bot=True
        ),
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id=str(CHANNEL_ID), name="general"),
        now_utc=datetime.now(UTC),
        **memory,
    )


def _followup_input(**memory) -> FollowupAgentInput:
    return FollowupAgentInput(
        me=Me(user_id="999", username="bot"),
        new_messages=[Message(message_id="102", author_id="200", body="and also?")],
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id=str(CHANNEL_ID), name="general"),
        now_utc=datetime.now(UTC),
        **memory,
    )


def _agent_result(output, *, input_tokens: int = 0, output_tokens: int = 0):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests=1,
        model="test-model",
    )
    return SimpleNamespace(
        output=output,
        usage=lambda: usage,
        all_messages=lambda: [],
        new_messages=lambda: [],
    )


def _turn_decision(message: str = "hi") -> TurnDecision:
    return TurnDecision(
        rankings=[MessageScore(message_id="101", score=10, reasoning="direct")],
        response_language="english",
        response=ResponseBody(target_message_id="101", message=message),
        topic="t",
        notes="n",
        continue_watching=True,
    )


def _briefing(*, remembered: list[str] | None = None) -> BriefingDecision:
    return BriefingDecision(
        rankings=[MessageScore(message_id="101", score=10, reasoning="direct")],
        brief=WriterBrief(
            message_summaries=["alice asked about shadows"],
            questions=['"how do soft shadows work?"'],
            response_language="english",
            remembered=remembered or [],
        ),
        topic="shadows",
        notes="alice: shadows",
        continue_watching=True,
    )


def _override(model_key: str = "gpt-5-4", *, drafter_model: str | None = None):
    return SimpleNamespace(
        model_key=model_key,
        drafter_model=drafter_model,
        daily_token_budget=1000,
        hourly_token_budget=0,
        reasoning_level=None,
        auto_respond=False,
        fallback_model_key=None,
        response_filter=None,
    )


@pytest.fixture
def fake_memory():
    memory = MagicMock()
    memory.reset_idle_counter = AsyncMock()
    memory.write_topic = AsyncMock()
    memory.write_notes = AsyncMock()
    memory.clear_notes = AsyncMock()
    memory.read_history = AsyncMock(return_value=[])
    memory.write_history = AsyncMock()
    memory.clear_history = AsyncMock()
    return memory


@pytest.fixture
async def event_redis():
    """A real (fake) Redis in the bot's byte-mode, so cursor logic is genuine."""
    client = fakeredis_aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.aclose()


def _make_engine(event_redis, memory_service, *, override=None):
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    override_service = MagicMock()
    override_service.get_override = AsyncMock(return_value=override or _override())
    bot.d = {
        "model_override_service": override_service,
        "guild_chat_memory_service": memory_service,
        "chat_memory_redis": event_redis,
    }

    async def _on_deactivate(channel_id: int) -> None:
        pass

    async def _noop_voice(channel_id, text, reply_to, instruction=None):
        return None

    engine = ChannelEngine(
        bot=bot,
        channel_id=CHANNEL_ID,
        guild_id=GUILD_ID,
        voice_send=_noop_voice,
        on_deactivate=_on_deactivate,
    )
    engine.activation_message = SimpleNamespace(
        id=101, author=SimpleNamespace(id=200, username="alice", is_bot=False)
    )
    return engine


def _memory_service(snapshot: GuildMemorySnapshot | Exception):
    service = MagicMock()
    if isinstance(snapshot, Exception):
        service.load_snapshot = AsyncMock(side_effect=snapshot)
    else:
        service.load_snapshot = AsyncMock(return_value=snapshot)
    service.save_note = AsyncMock()
    return service


def _queue_a_message(engine: ChannelEngine) -> None:
    """Give a follow-up turn something to react to."""
    from smarter_dev.bot.services.chat_engine import _QueuedMessage

    engine.queue.append(
        _QueuedMessage(
            message=SimpleNamespace(
                id=102, author=SimpleNamespace(id=200, username="alice", is_bot=False)
            ),
            enqueued_at=datetime.now(UTC),
        )
    )


class _EngineHarness:
    """Patches the engine's collaborators, capturing the builders' memory kwargs.

    The input builders are replaced with functions that really construct the
    agent input from the memory arguments the engine passes, so the assertions
    can read the FULLY RENDERED prompt rather than trusting a mock's call args.
    """

    def __init__(self, *, fake_memory, agent):
        self.fake_memory = fake_memory
        self.agent = agent
        self.initial_kwargs: dict = {}
        self.followup_kwargs: dict = {}
        self._patches: list = []

    async def _build_initial(self, **kwargs):
        self.initial_kwargs = kwargs
        return _initial_input(
            **{
                name: value
                for name, value in kwargs.items()
                if name
                in (
                    "long_term_memory",
                    "long_term_memory_updated_at",
                    "memory_notes",
                    "guild_events",
                )
            }
        )

    async def _build_followup(self, **kwargs):
        self.followup_kwargs = kwargs
        return _followup_input(
            **{
                name: value
                for name, value in kwargs.items()
                if name
                in (
                    "long_term_memory",
                    "long_term_memory_updated_at",
                    "new_guild_events",
                )
            }
        )

    def __enter__(self):
        self._patches = [
            patch(
                "smarter_dev.bot.services.chat_engine.get_chat_memory",
                return_value=self.fake_memory,
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.build_initial_input",
                new=self._build_initial,
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.build_followup_input",
                new=self._build_followup,
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.start_engagement",
                new=AsyncMock(return_value="engagement-1"),
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.persist_turn", new=AsyncMock()
            ),
            patch(
                "smarter_dev.bot.services.chat_engine.get_chat_agent",
                return_value=self.agent,
            ),
        ]
        for active in self._patches:
            active.__enter__()
        return self

    def __exit__(self, *exc_info):
        for active in reversed(self._patches):
            active.__exit__(*exc_info)
        return False

    @property
    def prompts(self) -> list[str]:
        return [call.kwargs["user_prompt"] for call in self.agent.run.await_args_list]


def _chat_agent() -> MagicMock:
    agent = MagicMock()
    agent.run = AsyncMock(return_value=_agent_result(_turn_decision()))
    return agent


# --------------------------------------------------------------------------- #
# Activation: the whole of memory goes out once
# --------------------------------------------------------------------------- #


async def test_initial_turn_carries_blob_notes_and_the_full_event_window(
    fake_memory, event_redis
):
    """The activation prompt renders all three layers, far to near."""
    await append_event(
        event_redis,
        mod_action_event(
            {
                "action_type": "timeout",
                "target_username": "mallory",
                "reason": "invite-link spam after a warning",
                "duration_seconds": 600,
                "source": "ai",
            },
            guild_id=str(GUILD_ID),
            channel_name="general",
        ),
    )
    service = _memory_service(_snapshot(notes=(_today_note(),)))
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)

    service.load_snapshot.assert_awaited_once_with(str(GUILD_ID))
    prompt = harness.prompts[0]
    assert '<what-i-remember updated="2026-08-06">' in prompt
    assert "hates cmake" in prompt
    assert "<from-today>" in prompt
    assert "soft shadows" in prompt
    assert '<what-i-did window="last-60-min">' in prompt
    assert "I timed out mallory for 10m in #general" in prompt


async def test_initial_turn_omits_memory_blocks_when_there_is_nothing_to_remember(
    fake_memory, event_redis
):
    """A brand-new guild gets no empty tags — an empty memory tag is an
    invitation to remark on its own amnesia."""
    service = _memory_service(EMPTY_SNAPSHOT)
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)

    prompt = harness.prompts[0]
    assert "<what-i-remember" not in prompt
    assert "<from-today>" not in prompt
    assert "<what-i-did" not in prompt


async def test_deps_carry_the_channel_name_and_engagement_for_the_remember_tool(
    fake_memory, event_redis
):
    """``remember`` denormalises the channel and soft-links the engagement, so
    both have to reach ``ChatDeps``."""
    service = _memory_service(EMPTY_SNAPSHOT)
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent):
        await engine._run_once(first_activation=True)

    deps = agent.run.await_args.kwargs["deps"]
    assert deps.channel_name == "general"
    assert deps.engagement_id == "engagement-1"
    assert deps.memories_saved_this_turn == 0
    assert deps.saved_memory_texts == []


# --------------------------------------------------------------------------- #
# Follow-ups: deltas only
# --------------------------------------------------------------------------- #


async def test_followup_sends_only_new_events_and_never_re_sends_the_blob(
    fake_memory, event_redis
):
    """The blob and today's notes ride the initial turn only — they stay in
    history and prompt-cache from there — while the event block narrows to the
    delta since the last turn."""
    await append_event(
        event_redis,
        bot_message_event(
            guild_id=str(GUILD_ID),
            summary="the weekly challenge announcement",
            channel_name="announcements",
        ),
    )
    service = _memory_service(_snapshot(notes=(_today_note(),)))
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)
        await append_event(
            event_redis,
            mod_action_event(
                {
                    "action_type": "warn",
                    "target_username": "mallory",
                    "reason": "second link drop",
                    "source": "manual",
                    "moderator_username": "zech",
                },
                guild_id=str(GUILD_ID),
                channel_name="general",
            ),
        )
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)

    followup_prompt = harness.prompts[1]
    assert "<what-i-remember" not in followup_prompt
    assert "<from-today>" not in followup_prompt
    assert '<what-i-did window="since-your-last-turn">' in followup_prompt
    assert "I warned mallory in #general for @zech" in followup_prompt
    # The activation's announcement already went out on turn one.
    assert "weekly challenge announcement" not in followup_prompt


async def test_event_cursor_never_delivers_the_same_action_twice(
    fake_memory, event_redis
):
    """A follow-up with nothing new renders no event block at all."""
    await append_event(
        event_redis,
        mod_action_event(
            {
                "action_type": "purge",
                "target_username": "mallory",
                "reason": "raid cleanup",
                "source": "ai",
            },
            guild_id=str(GUILD_ID),
        ),
    )
    service = _memory_service(EMPTY_SNAPSHOT)
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)

    assert "raid cleanup" in harness.prompts[0]
    assert "<what-i-did" not in harness.prompts[1]


async def test_memory_is_loaded_once_per_engagement_not_every_turn(
    fake_memory, event_redis
):
    service = _memory_service(_snapshot())
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent):
        await engine._run_once(first_activation=True)
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)

    assert service.load_snapshot.await_count == 1


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #


async def test_memory_load_failure_still_runs_the_turn(fake_memory, event_redis):
    """A memory outage costs the bot its memory for the turn, never the turn."""
    service = _memory_service(RuntimeError("memory backend is down"))
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)

    agent.run.assert_awaited_once()
    assert "<what-i-remember" not in harness.prompts[0]
    engine.bot.rest.create_message.assert_awaited_once()


async def test_event_log_failure_still_runs_the_turn(fake_memory, event_redis):
    """An unreadable event log renders no ``<what-i-did>`` and nothing else."""
    service = _memory_service(_snapshot())
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness, patch(
        "smarter_dev.bot.services.chat_engine.read_window",
        new=AsyncMock(side_effect=RedisError("no connection")),
    ):
        await engine._run_once(first_activation=True)

    agent.run.assert_awaited_once()
    assert "<what-i-did" not in harness.prompts[0]
    assert "<what-i-remember" in harness.prompts[0]
    # The cursor stayed unset, so the next activation still takes a full window.
    assert engine._event_cursor is None


async def test_engine_without_a_memory_service_runs_unchanged(
    fake_memory, event_redis
):
    engine = _make_engine(event_redis, None)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)

    agent.run.assert_awaited_once()
    assert "<what-i-remember" not in harness.prompts[0]


# --------------------------------------------------------------------------- #
# Compaction guard
# --------------------------------------------------------------------------- #


async def test_compaction_re_emits_the_blob_on_the_next_turn(
    fake_memory, event_redis
):
    """Compaction drains the history the blob was riding in, so the engine has
    to put it back — otherwise the bot's identity is amputated mid-engagement."""
    service = _memory_service(_snapshot())
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness, patch(
        "smarter_dev.bot.services.chat_engine.drain_collection",
        return_value=[SimpleNamespace(kind="compacted")],
    ):
        await engine._run_once(first_activation=True)
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)

    assert "<what-i-remember" in harness.prompts[1]
    assert "hates cmake" in harness.prompts[1]


async def test_a_re_emitted_blob_is_not_repeated_on_the_turn_after(
    fake_memory, event_redis
):
    """The flag is consumed: one compaction re-emits the blob exactly once."""
    service = _memory_service(_snapshot())
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()
    compaction_events = [SimpleNamespace(kind="compacted")]

    def _drain_once():
        # Only the first drained run compacted; later turns drain nothing.
        drained, compaction_events[:] = list(compaction_events), []
        return drained

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness, patch(
        "smarter_dev.bot.services.chat_engine.drain_collection",
        side_effect=_drain_once,
    ):
        await engine._run_once(first_activation=True)
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)

    assert "<what-i-remember" in harness.prompts[1]
    assert "<what-i-remember" not in harness.prompts[2]


async def test_no_compaction_means_no_re_emit(fake_memory, event_redis):
    service = _memory_service(_snapshot())
    engine = _make_engine(event_redis, service)
    agent = _chat_agent()

    with _EngineHarness(fake_memory=fake_memory, agent=agent) as harness:
        await engine._run_once(first_activation=True)
        _queue_a_message(engine)
        await engine._run_once(first_activation=False)

    assert "<what-i-remember" not in harness.prompts[1]


# --------------------------------------------------------------------------- #
# Two-stage: the writer's identity is copied, never re-derived
# --------------------------------------------------------------------------- #


async def test_writer_stage_receives_the_blob_verbatim(fake_memory, event_redis):
    """The blob is injected engine-side into the writer prompt — routing it
    through the cheap drafter would thrash the persona every turn."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_agent_result(
            _briefing(remembered=["alice has been on shaders all week"])
        )
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_agent_result(WriterOutput(message="Soft shadows, nice."))
    )
    service = _memory_service(_snapshot())
    engine = _make_engine(
        event_redis, service, override=_override("gemma-4-31b", drafter_model="gpt-5-4")
    )

    with _EngineHarness(fake_memory=fake_memory, agent=worker_agent), patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ):
        await engine._run_once(first_activation=True)

    writer_prompt = writer_agent.run.await_args.args[0]
    assert "What you remember about this place:" in writer_prompt
    assert "hates cmake" in writer_prompt
    assert "Also on your mind right now:" in writer_prompt
    assert "alice has been on shaders all week" in writer_prompt


async def test_writer_stage_omits_memory_sections_when_there_is_none(
    fake_memory, event_redis
):
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(return_value=_agent_result(_briefing()))
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_agent_result(WriterOutput(message="Sure."))
    )
    service = _memory_service(EMPTY_SNAPSHOT)
    engine = _make_engine(
        event_redis, service, override=_override("gemma-4-31b", drafter_model="gpt-5-4")
    )

    with _EngineHarness(fake_memory=fake_memory, agent=worker_agent), patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ):
        await engine._run_once(first_activation=True)

    writer_prompt = writer_agent.run.await_args.args[0]
    assert "What you remember about this place:" not in writer_prompt
    assert "Also on your mind right now:" not in writer_prompt
