"""Idle compaction of the embedded proactive history (#89).

A guild history unwritten for ``AGENT_VERBATIM_IDLE_WINDOW`` (2 hours)
becomes its memory note alone, with no verbatim message.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.privacy.purge import privacy_lock_key
from smarter_dev.bot.proactive.agent import KimiAgentRunner
from smarter_dev.bot.proactive.agent import is_summary_only
from smarter_dev.bot.proactive.agent import memory_note_pair
from smarter_dev.bot.proactive.history_store import IDLE_INDEX_KEY
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.services import chat_memory
from smarter_dev.shared.retention_policy import AGENT_VERBATIM_IDLE_WINDOW

GUILD = 2
IDLE = AGENT_VERBATIM_IDLE_WINDOW.total_seconds()
VERBATIM = "my cat Miso is sick"


def verbatim_wake() -> list:
    return [
        ModelRequest(parts=[UserPromptPart(f"[id=1] A·kai (uid=11): {VERBATIM}")]),
        ModelResponse(parts=[TextPart("told kai to see a vet")]),
    ]


def compacted_with_tail() -> list:
    return [*memory_note_pair("earlier: kai asked about cats"), *verbatim_wake()]


class Setup:
    def __init__(self, monkeypatch):
        self.redis = fakeredis.aioredis.FakeRedis()
        self.store = ProactiveHistoryStore(self.redis)
        self.runtime = proactive.ProactiveRuntime(
            SimpleNamespace(
                cache=SimpleNamespace(get_guild=lambda _gid: None),
                get_me=lambda: None,
                d={"chat_memory_redis": self.redis},
            ),
            start_consumers=False,
        )
        monkeypatch.setattr(self.runtime, "history_store", lambda: self.store)
        # No provider is built: the agent and its model are stand-ins.
        self.runtime._agent_model_id = "agent-model"
        monkeypatch.setattr(proactive, "build_twopass_model", lambda model_id: model_id)
        monkeypatch.setattr(
            proactive,
            "build_proactive_agent",
            lambda model, system_prompt: SimpleNamespace(),
        )
        self.summaries: list[list] = []

        async def summarize(messages) -> str:
            self.summaries.append(list(messages))
            return "kai asked about a sick cat in #general"

        monkeypatch.setattr(self.runtime, "compaction_summarize", summarize)

    async def backdate(self, seconds_ago: float) -> None:
        """As if the last write happened ``seconds_ago``."""
        await self.redis.zadd(IDLE_INDEX_KEY, {str(GUILD): time.time() - seconds_ago})

    async def stored(self) -> list:
        return await self.store.read_guild(GUILD)


@pytest.fixture
def setup(monkeypatch):
    return Setup(monkeypatch)


def dumped(history) -> str:
    return ModelMessagesTypeAdapter.dump_json(history).decode()


async def test_idle_history_is_folded_to_summary_only_at_two_hours(setup):
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.backdate(IDLE + 1)

    outcomes = await proactive.compact_idle_histories(setup.runtime)

    assert outcomes == {GUILD: "folded"}
    assert len(setup.summaries) == 1
    stored = await setup.stored()
    assert is_summary_only(stored)
    assert VERBATIM not in dumped(stored)
    assert await setup.redis.zscore(IDLE_INDEX_KEY, str(GUILD)) is None


async def test_history_inside_the_window_is_untouched(setup):
    # Negative control for the fold: a minute short of the window.
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.backdate(IDLE - 60)
    before = await setup.store.read_guild_raw(GUILD)

    assert await proactive.compact_idle_histories(setup.runtime) == {}
    assert setup.summaries == []
    assert await setup.store.read_guild_raw(GUILD) == before


async def test_a_write_while_waiting_for_the_lock_restarts_the_clock(setup):
    # The sweep listed the guild as idle, then waited on its wake lock while
    # a wake wrote: once held, the lock-side check sees the new write.
    await setup.store.write_guild(GUILD, verbatim_wake())
    before = await setup.store.read_guild_raw(GUILD)

    outcome = await proactive._compact_idle_guild(setup.runtime, setup.store, GUILD)

    assert outcome == "written since"
    assert setup.summaries == []
    assert await setup.store.read_guild_raw(GUILD) == before


async def test_freshly_compacted_history_drops_its_tail_without_a_model_call(setup):
    await setup.store.write_guild(GUILD, compacted_with_tail(), freshly_compacted=True)
    await setup.backdate(IDLE + 1)

    assert await proactive.compact_idle_histories(setup.runtime) == {
        GUILD: "tail dropped"
    }
    assert setup.summaries == []
    stored = await setup.stored()
    assert is_summary_only(stored)
    assert "earlier: kai asked about cats" in dumped(stored)
    assert VERBATIM not in dumped(stored)


async def test_unflagged_compacted_history_is_folded_by_the_model(setup):
    # Negative control for the flag: the same history without it.
    await setup.store.write_guild(GUILD, compacted_with_tail())
    await setup.backdate(IDLE + 1)

    assert await proactive.compact_idle_histories(setup.runtime) == {GUILD: "folded"}
    assert len(setup.summaries) == 1
    assert VERBATIM in dumped(setup.summaries[0])


async def test_any_later_write_clears_the_fresh_flag(setup):
    await setup.store.write_guild(GUILD, compacted_with_tail(), freshly_compacted=True)
    assert (await setup.store.guild_idle_state(GUILD))[1] is True
    await setup.store.write_guild(GUILD, [*compacted_with_tail(), *verbatim_wake()])
    assert (await setup.store.guild_idle_state(GUILD))[1] is False


async def test_the_in_memory_copy_takes_the_fold(setup):
    # A loaded runner must not write its verbatim RAM copy back next wake.
    state = setup.runtime.guild_state_for(GUILD)
    state.agent_runner = SimpleNamespace(history=verbatim_wake())
    state.history_loaded = True
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.backdate(IDLE + 1)

    await proactive.compact_idle_histories(setup.runtime)

    assert is_summary_only(state.agent_runner.history)


async def test_a_running_purge_defers_the_fold(setup):
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.backdate(IDLE + 1)
    await setup.redis.set(privacy_lock_key(str(GUILD)), "purge")

    assert await proactive.compact_idle_histories(setup.runtime) == {
        GUILD: "purge running"
    }
    assert VERBATIM in dumped(await setup.stored())


async def test_histories_from_before_the_index_start_their_clock(setup):
    # Written before this change: a history key with no index entry.
    await setup.redis.set(
        ProactiveHistoryStore._guild_history_key(GUILD),
        ModelMessagesTypeAdapter.dump_json(verbatim_wake()),
    )
    assert await setup.redis.zscore(IDLE_INDEX_KEY, str(GUILD)) is None

    assert await setup.store.index_unindexed_guild_histories(now=time.time()) == 1
    assert await setup.redis.zscore(IDLE_INDEX_KEY, str(GUILD)) is not None


async def test_the_ticker_folds_with_no_wake(setup, monkeypatch):
    # The sweep runs on its own clock, whether or not anything wakes.
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.backdate(IDLE + 1)
    monkeypatch.setattr(proactive, "runtime", setup.runtime)
    monkeypatch.setattr(
        proactive,
        "PROACTIVE_IDLE_SWEEP_TICK",
        SimpleNamespace(total_seconds=lambda: 0.01),
    )
    task = asyncio.create_task(proactive._idle_compaction_ticker())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if is_summary_only(await setup.stored()):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert is_summary_only(await setup.stored())


async def test_a_wake_stores_its_compaction_flagged(setup):
    state = setup.runtime.guild_state_for(GUILD)
    state.history_loaded = True
    runner = setup.runtime.agent_runner_for(state)

    compacted = compacted_with_tail()
    await runner.on_compacted(compacted)

    assert dumped(await setup.stored()) == dumped(compacted)
    assert (await setup.store.guild_idle_state(GUILD))[1] is True


async def test_an_unloaded_history_is_never_written_over(setup):
    # Negative control: a wake that could not load the stored history (it
    # may be unreadable) writes nothing early either.
    state = setup.runtime.guild_state_for(GUILD)
    state.history_loaded = False
    runner = setup.runtime.agent_runner_for(state)

    await runner.on_compacted(compacted_with_tail())

    assert await setup.store.read_guild_raw(GUILD) is None


class _FakeAgent:
    def __init__(self):
        self.started_from = None

    async def run(self, brief, *, deps, message_history):
        self.started_from = list(message_history or [])
        messages = [*self.started_from, ModelRequest(parts=[UserPromptPart(brief)])]
        usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_tokens=0)
        return SimpleNamespace(
            output="done", all_messages=lambda: messages, usage=usage
        )


async def test_runner_stores_the_compaction_before_the_model_runs():
    stored = []

    async def summarize(_messages):
        return "note"

    async def on_compacted(history):
        stored.append(list(history))

    agent = _FakeAgent()
    runner = KimiAgentRunner(
        agent=agent,
        summarize=summarize,
        token_limit=10,
        history=verbatim_wake() * 6,
        on_compacted=on_compacted,
    )
    await runner.wake("brief", deps=None)

    assert len(stored) == 1
    assert stored[0] == agent.started_from


async def test_runner_under_the_limit_stores_nothing_early():
    stored = []

    async def on_compacted(history):
        stored.append(history)

    runner = KimiAgentRunner(
        agent=_FakeAgent(),
        summarize=None,
        history=verbatim_wake(),
        on_compacted=on_compacted,
    )
    await runner.wake("brief", deps=None)
    assert stored == []


def test_chat_memory_keys_share_the_proactive_idle_window():
    seconds = int(AGENT_VERBATIM_IDLE_WINDOW.total_seconds())
    assert chat_memory.TOPIC_TTL_SECONDS == seconds == 7200
    assert chat_memory.NOTES_TTL_SECONDS == seconds
    assert chat_memory.HISTORY_TTL_SECONDS == seconds
    assert chat_memory.TOPIC_STALE_AFTER == AGENT_VERBATIM_IDLE_WINDOW


async def _legacy_world(redis):
    store = ProactiveHistoryStore(redis)
    await store.write(7, verbatim_wake())  # legacy per-channel key
    await store.write(8, verbatim_wake())
    await store.write_guild(GUILD, verbatim_wake())
    # Keys the legacy pattern also matches, or sits beside: never deleted.
    keep = {
        b"proactive:v1:{guild:2}:history": b"worker",
        b"chat_agent:7:history": b"chat",
        b"proactive:7:cursor": b"cursor",
    }
    for key, value in keep.items():
        await redis.set(key, value)
    return store, keep


async def test_the_first_tick_deletes_the_legacy_channel_histories(setup, monkeypatch):
    _store, keep = await _legacy_world(setup.redis)
    monkeypatch.setattr(proactive, "runtime", setup.runtime)
    monkeypatch.setattr(
        proactive,
        "PROACTIVE_IDLE_SWEEP_TICK",
        SimpleNamespace(total_seconds=lambda: 0.01),
    )
    task = asyncio.create_task(proactive._idle_compaction_ticker())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not await setup.redis.exists("proactive:7:history", "proactive:8:history"):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert await setup.redis.exists("proactive:7:history", "proactive:8:history") == 0
    for key, value in keep.items():
        assert await setup.redis.get(key) == value
    assert VERBATIM in dumped(await setup.stored())  # not idle: untouched


async def test_a_purge_racing_the_sweep_cannot_restore_a_legacy_key(monkeypatch):
    # The purge read a legacy key, then the sweep deleted it before the
    # purge wrote its rewrite back: the rewrite must not recreate it.
    from tests.bot.privacy_purge_test import LIVE_CHANNEL
    from tests.bot.privacy_purge_test import _command
    from tests.bot.privacy_purge_test import _consume_once
    from tests.bot.privacy_purge_test import _publish
    from tests.bot.privacy_purge_test import build_world

    world = await build_world()
    read_raw = ProactiveHistoryStore.read_raw

    async def read_then_swept(self, channel_id):
        raw = await read_raw(self, channel_id)
        await self.delete_legacy_channel_histories()
        return raw

    monkeypatch.setattr(ProactiveHistoryStore, "read_raw", read_then_swept)
    await _publish(world.redis, _command())
    await _consume_once(world)

    assert world.acks[0][1].outcome == "purged"
    assert await world.redis.exists(f"proactive:{LIVE_CHANNEL}:history") == 0


async def test_a_purge_with_no_legacy_keys_left_completes():
    from tests.bot.privacy_purge_test import _command
    from tests.bot.privacy_purge_test import _consume_once
    from tests.bot.privacy_purge_test import _publish
    from tests.bot.privacy_purge_test import build_world

    world = await build_world()
    assert await world.store.delete_legacy_channel_histories() == 2
    await _publish(world.redis, _command())
    await _consume_once(world)

    assert world.acks[0][1].outcome == "purged"
    assert await world.redis.keys("proactive:1*:history") == []


async def test_a_purge_rewrite_keeps_the_idle_clock():
    # Idle for a minute short of the window, then purged: the rest of the
    # history must still fold on time, not 2 hours after the purge.
    from tests.bot.privacy_purge_test import GUILD as PURGE_GUILD
    from tests.bot.privacy_purge_test import _command
    from tests.bot.privacy_purge_test import _consume_once
    from tests.bot.privacy_purge_test import _publish
    from tests.bot.privacy_purge_test import build_world

    world = await build_world()
    written_at = time.time() - IDLE + 60
    await world.redis.zadd(IDLE_INDEX_KEY, {PURGE_GUILD: written_at})
    before = await world.store.read_guild_raw(int(PURGE_GUILD))

    await _publish(world.redis, _command())
    await _consume_once(world)

    assert world.acks[0][1].outcome == "purged"
    assert await world.store.read_guild_raw(int(PURGE_GUILD)) != before
    assert await world.redis.zscore(IDLE_INDEX_KEY, PURGE_GUILD) == written_at


async def test_an_unreadable_history_is_deleted_at_the_idle_point(setup, caplog):
    secret = b"not json: " + VERBATIM.encode()
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.redis.set(ProactiveHistoryStore._guild_history_key(GUILD), secret)
    await setup.backdate(IDLE + 1)
    state = setup.runtime.guild_state_for(GUILD)
    state.agent_runner = SimpleNamespace(history=verbatim_wake())
    state.history_loaded = True

    with caplog.at_level("INFO"):
        outcomes = await proactive.compact_idle_histories(setup.runtime)

    assert outcomes == {GUILD: "unreadable deleted"}
    assert setup.summaries == []
    assert await setup.store.read_guild_raw(GUILD) is None
    assert await setup.redis.zscore(IDLE_INDEX_KEY, str(GUILD)) is None
    assert state.agent_runner.history == []
    assert "unreadable deleted" in caplog.text
    assert VERBATIM not in caplog.text


async def test_an_unreadable_history_inside_the_window_is_kept(setup):
    # Negative control for the delete: only the idle point removes it.
    await setup.store.write_guild(GUILD, verbatim_wake())
    await setup.redis.set(ProactiveHistoryStore._guild_history_key(GUILD), b"{bad")
    await setup.backdate(IDLE - 60)

    assert await proactive.compact_idle_histories(setup.runtime) == {}
    assert await setup.store.read_guild_raw(GUILD) == b"{bad"
