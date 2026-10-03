"""The bot's privacy purge: every store it holds, rewritten without one person.

Synthetic target kai (111111111111111111) and bystander nia
(222222222222222222). The "model" is a FunctionModel standing in for the
agent's own summarizer: it writes nia's part back and leaves kai out, so the
tests check plumbing, locking, validation and failure handling, never a real
provider.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis
import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.privacy import purge
from smarter_dev.bot.proactive.agent import KimiAgentRunner
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WatchInstruction
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.bot.services.proactive_settings_service import EnabledProactiveChannel
from smarter_dev.shared.privacy_purge import BOT_CONSUMER_GROUP
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import purge_epoch_key

KAI = "111111111111111111"
NIA = "222222222222222222"
GUILD = "333333333333333333"
OTHER_GUILD = "444444444444444444"
LIVE_CHANNEL = 10  # live chat engine in GUILD
STORED_CHANNEL = 11  # only Redis keys, GUILD per the bot cache
OTHER_CHANNEL = 12  # OTHER_GUILD: must stay untouched
UNPLACED_CHANNEL = 13  # guild unknown to the cache: purged anyway

NIA_SUMMARY = f"nia (id {NIA}) asked how to benchmark tokio; unresolved."
NIA_TOPIC = "Benchmarking tokio."
NIA_NOTES = "nia wants numbers for tokio."
NIA_NOTE = f"nia (id {NIA}) in #general asked about tokio benchmarks."


def _chat_history(channel_id: int) -> list:
    return [
        ModelRequest(
            parts=[
                SystemPromptPart("you are the chat agent"),
                UserPromptPart(
                    f'<message id="1" user-id="{NIA}" username="nia">\n'
                    f"how do I benchmark tokio? (#{channel_id})\n</message>"
                ),
            ]
        ),
        ModelResponse(parts=[TextPart("use criterion")]),
        ModelRequest(
            parts=[
                UserPromptPart(
                    f'<message id="2" user-id="{KAI}" username="kai">\n'
                    "kai here: my secret benchmark is 3x faster\n</message>"
                )
            ]
        ),
        ModelResponse(parts=[TextPart("nice one kai")]),
    ]


def _proactive_history() -> list:
    return [
        ModelRequest(
            parts=[
                UserPromptPart(
                    f"[2026-10-03T10:00:00Z] [id=5] A·nia (uid={NIA}): tokio?\n"
                    f"[2026-10-03T10:01:00Z] [id=6] B·kai (uid={KAI}): "
                    "kai's private numbers"
                )
            ]
        ),
        ModelResponse(parts=[TextPart("kai shared numbers; nia asked about tokio")]),
    ]


class _Settings:
    """Watch instructions for one enabled channel of GUILD."""

    def __init__(self, store: InstructionStore):
        self.stored = store.to_stored()
        self.saved: list[str] = []

    async def list_enabled_channels(self, guild_id):
        assert guild_id == GUILD
        return [
            EnabledProactiveChannel(
                channel_id=str(LIVE_CHANNEL), watch_addendum=self.stored
            )
        ]

    async def set_watch_addendum(self, guild_id, channel_id, addendum):
        self.saved.append(addendum)
        self.stored = addendum


def _instruction_store() -> InstructionStore:
    expires = datetime.now(UTC) + timedelta(hours=2)
    return InstructionStore(
        seed="seed",
        entries=[
            WatchInstruction("w1", "wake when kai reports his benchmark", expires),
            WatchInstruction("w2", "wake for tokio release questions", expires),
        ],
        _next_id=3,
    )


def _prompt_text(messages) -> str:
    return ModelMessagesTypeAdapter.dump_json(messages).decode()


class _Summarizer:
    """Plays the agent's own model for every purge prompt.

    ``mode``: "good" writes nia's part only; "leak_id" always keeps kai's id;
    "leak_name_once" names kai on the first attempt only; "leak_name" always.
    """

    def __init__(self, mode: str = "good"):
        self.mode = mode
        self.calls: list[str] = []
        self.model = FunctionModel(self._respond)

    def _respond(self, messages, info: AgentInfo) -> ModelResponse:
        text = _prompt_text(messages)
        self.calls.append(text)
        attempt_suffix = ""
        if self.mode == "leak_id" or (
            self.mode == "leak_name_once" and "CORRECTION" not in text
        ):
            attempt_suffix = (
                f" also kai (id {KAI})" if self.mode == "leak_id" else " and Kai."
            )
        if self.mode == "leak_name":
            attempt_suffix = " and Kai."
        if "You maintain the watch instructions" in text:
            if self.mode == "good":
                first = {"instruction_id": "w1", "action": "drop", "text": ""}
            else:
                first = {
                    "instruction_id": "w1",
                    "action": "rewrite",
                    "text": "wake for benchmark reports" + attempt_suffix,
                }
            listed = text.split("INSTRUCTIONS:", 1)[1]
            decisions = [
                {"instruction_id": "w2", "action": "keep", "text": ""}
            ]
            if "- w1:" in listed:
                decisions.insert(0, first)
            return self._tool(info, {"decisions": decisions})
        if "rewrite a Discord chat agent's working memory" in text:
            return self._tool(
                info,
                {
                    "summary": NIA_SUMMARY + attempt_suffix,
                    "topic": NIA_TOPIC,
                    "notes": NIA_NOTES,
                },
            )
        assert "being compacted because a person asked" in text
        return ModelResponse(parts=[TextPart(NIA_NOTE + attempt_suffix)])

    @staticmethod
    def _tool(info: AgentInfo, payload: dict) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, payload)]
        )


@pytest.fixture
async def world(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    memory = ChatMemory(redis)
    for channel in (LIVE_CHANNEL, STORED_CHANNEL, OTHER_CHANNEL, UNPLACED_CHANNEL):
        await memory.write_history(channel, _chat_history(channel))
        await memory.write_topic(channel, "kai's benchmark vs tokio")
        await memory.write_notes(channel, f"kai ({KAI}) and nia compare numbers")
    history_store = ProactiveHistoryStore(redis)
    await history_store.write_guild(int(GUILD), _proactive_history())
    await history_store.write(LIVE_CHANNEL, _proactive_history())  # legacy key
    await history_store.write(OTHER_CHANNEL, _proactive_history())

    settings = _Settings(_instruction_store())
    bot = SimpleNamespace(
        d={"proactive_settings_service": settings},
        cache=SimpleNamespace(get_guilds_view=lambda: {}),
    )
    run = proactive.ProactiveRuntime(bot, start_consumers=False)
    guild_state = run.guild_state_for(int(GUILD))
    guild_state.agent_runner = KimiAgentRunner(
        agent=None, summarize=None, history=_proactive_history()
    )
    guild_state.history_loaded = True
    guild_state.memory_refreshed_at = 123.0
    guild_state.queue.push(
        Notification(kind="mention", created_at=datetime.now(UTC),
                     body=f"You were @mentioned by kai (id {KAI})")
    )
    guild_state.queue.push(
        Notification(kind="mention", created_at=datetime.now(UTC),
                     body=f"You were @mentioned by nia (id {NIA})")
    )

    engine = SimpleNamespace(
        channel_id=LIVE_CHANNEL, guild_id=int(GUILD), run_lock=asyncio.Lock()
    )
    guilds = {
        LIVE_CHANNEL: GUILD,
        STORED_CHANNEL: GUILD,
        OTHER_CHANNEL: OTHER_GUILD,
    }
    acks: list[tuple[str, object]] = []
    summarizer = _Summarizer()

    async def post_ack(run_id, ack):
        acks.append((run_id, ack))
        return True

    async def engines():
        return [engine]

    deps = purge.PurgeDeps(
        redis=redis,
        chat_memory=memory,
        chat_engines=engines,
        channel_guild=guilds.get,
        proactive=lambda: run,
        chat_model=lambda: summarizer.model,
        proactive_model=lambda: summarizer.model,
        post_ack=post_ack,
    )
    await purge.ensure_group(redis)
    return SimpleNamespace(
        redis=redis,
        memory=memory,
        store=history_store,
        run=run,
        guild_state=guild_state,
        engine=engine,
        settings=settings,
        deps=deps,
        acks=acks,
        summarizer=summarizer,
    )


def _command(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "user_id": KAI,
        "names": ["kai", "Kai the Rustacean"],
        "guild_ids": [GUILD],
        "created_at": "2026-10-03T12:00:00Z",
    }
    payload.update(overrides)
    return payload


async def _publish(redis, payload) -> bytes:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return await redis.xadd(PURGE_STREAM, {"payload": body})


async def _consume_once(world) -> None:
    entries = await purge.read_batch(world.redis, "test-consumer", block_ms=10)
    for stream_id, fields in entries:
        await purge.process_entry(world.deps, stream_id, fields)


async def _pending(redis) -> int:
    info = await redis.xpending(PURGE_STREAM, BOT_CONSUMER_GROUP)
    return info["pending"]


async def _snapshot(redis) -> dict:
    keys = sorted(
        k for k in await redis.keys("*") if not k.startswith(b"privacy:")
        and b"purge-epoch" not in k
    )
    return {key: await redis.get(key) for key in keys}


def _assert_clean(text: str) -> None:
    assert KAI not in text
    assert "kai" not in text.lower()


async def test_purge_removes_kai_from_every_store_and_keeps_nia(world):
    await _publish(world.redis, _command())

    await _consume_once(world)

    # chat: live engine's channel, a stored channel and an unplaced one
    for channel in (LIVE_CHANNEL, STORED_CHANNEL, UNPLACED_CHANNEL):
        history = await world.memory.read_history(channel)
        text = _prompt_text(history)
        _assert_clean(text)
        assert NIA_SUMMARY in text
        assert "you are the chat agent" in text  # system prompt survives
        assert len(history) == 1
        assert (await world.memory.get_topic(channel)).text == NIA_TOPIC
        assert await world.memory.get_notes(channel) == NIA_NOTES
    # the other guild's channel is untouched
    other = _prompt_text(await world.memory.read_history(OTHER_CHANNEL))
    assert KAI in other
    assert KAI in _prompt_text(await world.store.read(OTHER_CHANNEL))

    # embedded proactive: Redis and RAM, legacy per-channel key
    for history in (
        await world.store.read_guild(int(GUILD)),
        world.guild_state.agent_runner.history,
        await world.store.read(LIVE_CHANNEL),
    ):
        text = _prompt_text(history)
        _assert_clean(text)
        assert NIA_NOTE in text
        assert len(history) == 2  # the memory-note pair, no verbatim tail
    assert world.guild_state.memory_refreshed_at == 0.0
    assert [n.body for n in world.guild_state.queue.items] == [
        f"You were @mentioned by nia (id {NIA})"
    ]

    # watch instructions: kai's dropped, nia's kept, persisted once
    assert len(world.settings.saved) == 1
    saved = json.loads(world.settings.saved[0])
    assert [entry["id"] for entry in saved] == ["w2"]

    # epoch, ack, XACK
    assert await world.redis.get(purge_epoch_key(GUILD)) == b"1"
    assert len(world.acks) == 1
    run_id, ack = world.acks[0]
    assert ack.component == "bot" and ack.guild_id == GUILD
    assert ack.outcome == "purged"
    assert set(ack.stores) >= {
        purge.STORE_CHAT_HISTORY,
        purge.STORE_CHAT_TOPIC,
        purge.STORE_CHAT_NOTES,
        purge.STORE_PROACTIVE_GUILD_HISTORY,
        purge.STORE_PROACTIVE_CHANNEL_HISTORY,
        purge.STORE_WATCH_INSTRUCTIONS,
    }
    _assert_clean(ack.model_dump_json())
    assert await _pending(world.redis) == 0

    # the model was told whom to remove, privately, in every call
    assert world.summarizer.calls
    assert all(KAI in call for call in world.summarizer.calls)


async def test_purge_keeps_the_original_expiry(world):
    before = await world.redis.ttl(f"chat_agent:{LIVE_CHANNEL}:history")
    await _publish(world.redis, _command())

    await _consume_once(world)

    after = await world.redis.ttl(f"chat_agent:{LIVE_CHANNEL}:history")
    assert 0 < after <= before


async def test_failing_summarizer_leaves_every_store_untouched(world):
    world.summarizer.mode = "leak_id"
    before = await _snapshot(world.redis)
    ram_before = list(world.guild_state.agent_runner.history)
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await _snapshot(world.redis) == before
    assert world.guild_state.agent_runner.history == ram_before
    assert world.settings.saved == []
    _run_id, ack = world.acks[0]
    assert ack.outcome == "failed"
    _assert_clean(ack.model_dump_json())
    assert "failed=" in ack.detail
    # 1 attempt + 2 retries per store, nothing more
    chat_calls = [
        c for c in world.summarizer.calls if "working memory for one" in c
    ]
    assert len(chat_calls) == 3 * 3  # three chat channels
    assert await _pending(world.redis) == 0  # acked (as failed), so XACKed


async def test_model_error_also_leaves_store_untouched(world):
    def broken(messages, info):
        raise RuntimeError("provider down")

    world.deps.chat_model = lambda: FunctionModel(broken)
    world.deps.proactive_model = lambda: FunctionModel(broken)
    before = await _snapshot(world.redis)
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await _snapshot(world.redis) == before
    assert world.acks[0][1].outcome == "failed"


async def test_name_hit_gets_one_reask_then_is_reported(world):
    world.summarizer.mode = "leak_name_once"
    await _publish(world.redis, _command())

    await _consume_once(world)

    _run_id, ack = world.acks[0]
    assert ack.outcome == "purged"
    assert "name mentions" not in ack.detail  # the re-ask cleaned it
    corrections = [c for c in world.summarizer.calls if "CORRECTION" in c]
    assert corrections


async def test_persistent_name_hit_is_accepted_and_counted(world):
    world.summarizer.mode = "leak_name"
    await _publish(world.redis, _command())

    await _consume_once(world)

    _run_id, ack = world.acks[0]
    assert ack.outcome == "purged"
    assert "name mentions kept after re-ask:" in ack.detail
    _assert_clean(ack.detail.replace("name mentions", ""))
    text = _prompt_text(await world.memory.read_history(LIVE_CHANNEL))
    assert KAI not in text


async def test_second_purge_is_safe(world):
    await _publish(world.redis, _command())
    await _consume_once(world)
    await _publish(world.redis, _command())
    await _consume_once(world)

    assert [ack.outcome for _run, ack in world.acks] == ["purged", "purged"]
    _assert_clean(_prompt_text(await world.memory.read_history(LIVE_CHANNEL)))
    _assert_clean(_prompt_text(world.guild_state.agent_runner.history))
    assert await world.redis.get(purge_epoch_key(GUILD)) == b"2"
    assert await _pending(world.redis) == 0


async def test_purge_waits_for_the_live_engine_turn(world):
    await _publish(world.redis, _command())
    await world.engine.run_lock.acquire()
    task = asyncio.create_task(_consume_once(world))
    await asyncio.sleep(0.05)

    assert KAI in _prompt_text(await world.memory.read_history(LIVE_CHANNEL))
    assert not task.done()
    world.engine.run_lock.release()
    await task

    _assert_clean(_prompt_text(await world.memory.read_history(LIVE_CHANNEL)))


async def test_purge_waits_for_the_proactive_wake(world):
    await _publish(world.redis, _command())
    await world.guild_state.wake_lock.acquire()
    task = asyncio.create_task(_consume_once(world))
    await asyncio.sleep(0.05)

    assert KAI in _prompt_text(world.guild_state.agent_runner.history)
    world.guild_state.wake_lock.release()
    await task

    _assert_clean(_prompt_text(world.guild_state.agent_runner.history))


async def test_external_guild_keeps_its_watch_instructions(world, monkeypatch):
    monkeypatch.setattr(
        world.run, "execution_mode_for", lambda guild_id: "external"
    )
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert world.settings.saved == []
    assert world.acks[0][1].outcome == "purged"


async def test_malformed_payload_is_dropped_without_content(world, caplog):
    await _publish(world.redis, _command(extra_field=KAI))
    await _publish(world.redis, "not json kai")

    await _consume_once(world)

    assert world.acks == []
    assert await _pending(world.redis) == 0
    assert KAI not in caplog.text and "kai" not in caplog.text.lower()


async def test_unknown_run_is_acked_and_dropped(world):
    async def unknown(run_id, ack):
        return False

    world.deps.post_ack = unknown
    await _publish(world.redis, _command(guild_ids=[GUILD, OTHER_GUILD]))

    await _consume_once(world)

    assert await _pending(world.redis) == 0


async def test_failed_ack_post_leaves_entry_pending_for_reclaim(world):
    async def down(run_id, ack):
        raise ConnectionError("web down")

    world.deps.post_ack = down
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await _pending(world.redis) == 1


async def test_group_creation_tolerates_busygroup(world):
    await purge.ensure_group(world.redis)  # already created by the fixture
    await purge.ensure_group(world.redis)


async def test_logs_never_carry_the_target(world, caplog):
    world.summarizer.mode = "leak_id"
    caplog.set_level("DEBUG")
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert KAI not in caplog.text
    assert "kai" not in caplog.text.lower()


def test_plugin_wires_live_bot_into_purge_deps():
    from smarter_dev.bot.plugins import privacy

    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda cid: SimpleNamespace(guild_id=int(GUILD))
            if cid == LIVE_CHANNEL
            else None,
            get_thread=lambda cid: None,
        ),
    )
    assert privacy.build_purge_deps(bot) is None  # no Redis, no bot API

    bot.d["chat_memory_redis"] = fakeredis.aioredis.FakeRedis()
    bot.d["privacy_service"] = SimpleNamespace(post_purge_ack=AsyncMockAck())
    deps = privacy.build_purge_deps(bot)

    assert deps.channel_guild(LIVE_CHANNEL) == GUILD
    assert deps.channel_guild(UNPLACED_CHANNEL) is None
    assert deps.proactive() is proactive.runtime


class AsyncMockAck:
    async def __call__(self, run_id, ack):
        return True
