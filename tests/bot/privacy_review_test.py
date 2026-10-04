"""PR 134 review items for the bot purge, one or more tests per item.

Reuses the purge test world: kai (target) and nia (bystander), a fake
summarizer standing in for the agents' own models, fakeredis.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.agents import chat_compaction
from smarter_dev.bot.privacy import attribution
from smarter_dev.bot.privacy import compaction
from smarter_dev.bot.privacy import purge
from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import consumer_key
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.privacy.blocked_users import redact_blocked_mentions
from smarter_dev.bot.proactive.agent import memory_note_pair
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.shared.privacy_purge import BOT_CONSUMER_GROUP
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import WORKER_CONSUMER_GROUP
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.privacy_purge import purge_epoch_key
from tests.bot.privacy_purge_test import CHAT_MARK
from tests.bot.privacy_purge_test import GUILD
from tests.bot.privacy_purge_test import KAI
from tests.bot.privacy_purge_test import LIVE_CHANNEL
from tests.bot.privacy_purge_test import NIA
from tests.bot.privacy_purge_test import NIA_NOTE
from tests.bot.privacy_purge_test import OTHER_GUILD
from tests.bot.privacy_purge_test import PROACTIVE_MARK
from tests.bot.privacy_purge_test import STORED_CHANNEL
from tests.bot.privacy_purge_test import UNPLACED_CHANNEL
from tests.bot.privacy_purge_test import _assert_clean
from tests.bot.privacy_purge_test import _calls
from tests.bot.privacy_purge_test import _command
from tests.bot.privacy_purge_test import _consume_once
from tests.bot.privacy_purge_test import _pending
from tests.bot.privacy_purge_test import _prompt_text
from tests.bot.privacy_purge_test import _publish
from tests.bot.privacy_purge_test import build_world

TARGET = PurgeTarget.build(KAI, ["kai"])
REAL_PROMPT = (
    Path(chat_compaction.__file__).parent / "prompts" / "chat_agent.md"
).read_text()


@pytest.fixture
async def world():
    return await build_world()


def _now() -> datetime:
    return datetime.now(UTC)


# -- 1. producer buffer re-checked at fire time --------------------------------


async def test_buffered_message_of_newly_blocked_author_never_reaches_watcher(
    monkeypatch,
):
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.proactive.watcher import WatcherDecision
    from tests.bot.privacy_model_input_test import KAI_USER
    from tests.bot.privacy_model_input_test import NIA_USER
    from tests.bot.privacy_model_input_test import _fake_bot
    from tests.bot.privacy_model_input_test import _message
    from tests.bot.privacy_model_input_test import _Settings

    blocked = get_blocked_users()
    blocked.load(1, [])  # kai not blocked yet
    monkeypatch.setenv(proactive.WATCHER_MODEL_ENV_VAR, "z-ai/glm-5.3-flash")
    monkeypatch.setenv(proactive.EXTERNAL_GUILDS_ENV_VAR, "2")
    bot = _fake_bot([], d={"proactive_settings_service": _Settings()})
    run = proactive.ProactiveRuntime(bot, start_consumers=False)
    monkeypatch.setattr(proactive, "runtime", run)
    published = []
    run.redis_notification_queue = lambda: SimpleNamespace(
        set_execution_owner=AsyncMock(),
        publish=AsyncMock(side_effect=published.append),
        publish_shadow=AsyncMock(side_effect=published.append),
    )
    captured = {}

    class _Parrot:
        async def decide(self, **kwargs):
            captured.update(kwargs)
            return (
                WatcherDecision(
                    wake=True,
                    reason="parrot",
                    summary=kwargs["new_transcript"],
                    relevant_message_ids=list(kwargs["new_message_ids"]),
                ),
                {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
            )

    monkeypatch.setattr(run, "watcher", lambda: _Parrot())
    state = run.state_for(2, 1)
    state.buffer.append(
        proactive.channel_message_from_hikari(
            _message(3001, KAI_USER, "kai's buffered secret")
        )
    )
    state.buffer.append(
        proactive.channel_message_from_hikari(
            _message(3002, NIA_USER, f"hey <@{KAI}> nice", minutes=1)
        )
    )
    blocked.load(2, [KAI])  # now the purge flow blocks kai

    await proactive._run_producer_once(state, passive=True)

    seen = json.dumps(captured, default=str) + "".join(
        envelope.model_dump_json() for envelope in published
    )
    assert "buffered secret" not in seen
    assert KAI not in seen and "3001" not in seen
    assert "@[blocked user] nice" in seen


async def test_purge_clears_target_messages_from_watcher_buffers(world):
    from smarter_dev.bot.proactive.types import ChannelMessage

    def message(id_, author, content):
        return ChannelMessage(
            id=id_, timestamp=_now(), author_id=author, author_name="x",
            author_display="x", is_bot=False, content=content, reply_to_id=None,
            mention_user_ids=(), mention_everyone=False, attachment_count=0,
            sticker_count=0, message_type=0,
        )

    state = world.run.state_for(int(GUILD), LIVE_CHANNEL)
    state.buffer = [
        message("1", KAI, "mine"),
        message("2", NIA, f"<@{KAI}> hi"),
        message("3", NIA, "tokio?"),
    ]
    state.buffer_arrivals = [1.0, 2.0, 3.0]
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert [m.id for m in state.buffer] == ["3"]
    assert state.buffer_arrivals == [3.0]


# -- 2. one error never kills the consumer -------------------------------------


async def test_corrupt_done_record_is_ignored_not_poison(world):
    payload = _command()
    await world.redis.hset(
        purge.purge_done_key(payload["run_id"]), f"guild:{GUILD}", "{not json"
    )
    await _publish(world.redis, payload)

    await _consume_once(world)

    assert world.acks and world.acks[0][1].outcome == "purged"
    assert await _pending(world.redis) == 0


async def test_redis_error_mid_entry_leaves_it_pending_and_returns(world):
    original = world.redis.xack
    world.redis.xack = AsyncMock(side_effect=ConnectionError("redis down"))
    await _publish(world.redis, _command())

    await _consume_once(world)  # must not raise

    world.redis.xack = original
    assert await _pending(world.redis) == 1


async def test_loop_survives_errors_and_heartbeats_only_after_good_polls(
    monkeypatch,
):
    redis = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(purge, "ERROR_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(purge, "IDLE_SECONDS", 0.01)
    reads = []
    healthy = []

    async def read(*args, **kwargs):
        reads.append(1)
        if not healthy:
            raise ConnectionError("redis blip")
        await asyncio.sleep(0.005)
        return []

    monkeypatch.setattr(purge, "read_batch", read)
    deps = SimpleNamespace(redis=redis)
    task = asyncio.create_task(purge.purge_consumer_loop(deps))
    await asyncio.sleep(0.08)

    assert not task.done()  # survived repeated errors
    assert len(reads) >= 2
    assert await redis.get(consumer_key()) is None  # no good poll yet

    healthy.append(True)
    await asyncio.sleep(0.05)
    assert await redis.get(consumer_key()) == b"1"
    assert 0 < await redis.ttl(consumer_key()) <= 180

    # A standby (not acting) never heartbeats.
    monkeypatch.setattr(purge.leadership, "is_acting", lambda: False)
    await asyncio.sleep(0.03)  # let an iteration already polling finish
    await redis.delete(consumer_key())
    await asyncio.sleep(0.05)
    assert await redis.get(consumer_key()) is None
    task.cancel()


async def test_missing_group_is_recreated_at_zero(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(purge, "IDLE_SECONDS", 0.01)
    await redis.xadd(PURGE_STREAM, {"payload": "not json"})
    deps = SimpleNamespace(redis=redis)
    task = asyncio.create_task(purge.purge_consumer_loop(deps, block_ms=5))
    await asyncio.sleep(0.05)
    # Someone destroys the group: the loop recreates it from id 0.
    await redis.xgroup_destroy(PURGE_STREAM, BOT_CONSUMER_GROUP)
    await redis.xadd(PURGE_STREAM, {"payload": "also not json"})
    await asyncio.sleep(0.1)
    task.cancel()

    groups = {g["name"]: g for g in await redis.xinfo_groups(PURGE_STREAM)}
    assert BOT_CONSUMER_GROUP.encode() in groups
    # Both entries were delivered again after the recreate (id 0).
    assert groups[BOT_CONSUMER_GROUP.encode()]["entries-read"] == 2


async def test_supervisor_restarts_a_dead_consumer():
    starts = []

    async def dying():
        starts.append(1)
        if len(starts) < 3:
            raise RuntimeError("bug")
        await asyncio.sleep(10)

    task = asyncio.create_task(purge.supervise(dying, backoff=0.01))
    await asyncio.sleep(0.1)

    assert len(starts) == 3 and not task.done()
    task.cancel()


# -- 3. chat skip rule with the real system prompt -----------------------------


def test_real_chat_prompt_does_not_force_a_fold():
    assert "<message" in REAL_PROMPT  # the literal tag that used to trip it
    history = [
        ModelRequest(
            parts=[
                SystemPromptPart(REAL_PROMPT),
                UserPromptPart(
                    f'<message id="1" user-id="{NIA}" username="nia">\ntokio?\n'
                    "</message>"
                ),
            ]
        ),
        ModelResponse(parts=[TextPart("use criterion")]),
    ]
    assert purge.chat_memory_is_clean(history, "tokio", None, TARGET)

    # The system part is still searched for the person.
    history[0].parts[0] = SystemPromptPart(REAL_PROMPT + f"\nremember {KAI}")
    assert not purge.chat_memory_is_clean(history, None, None, TARGET)


def test_unmarked_chat_summary_counts_as_unattributed():
    marked = ModelRequest(
        parts=[UserPromptPart(f"[compacted history] {attribution.ATTRIBUTION_MARK} x")]
    )
    unmarked = ModelRequest(parts=[UserPromptPart("[compacted history] x")])
    assert purge.chat_memory_is_clean([marked], None, None, TARGET)
    assert not purge.chat_memory_is_clean([unmarked], None, None, TARGET)
    assert attribution.CHAT_SUMMARY_PREFIX == chat_compaction.COMPACTED_PREFIX


# -- 4. proactive skip rule ----------------------------------------------------


def _uid_line(text: str) -> str:
    return f"[2026-10-03T10:00:00Z] [id=9] A·nia (uid={NIA}): {text}"


async def test_compacted_pre_uid_history_with_fresh_tail_is_folded(world):
    """An unmarked memory note (written before attribution, mentioning kai
    only as "kaizen") plus a fresh uid= tail: folded on the first run."""
    history = [
        *memory_note_pair("kaizen promised benchmarks to nia"),
        ModelRequest(parts=[UserPromptPart(_uid_line("tokio?"))]),
        ModelResponse(parts=[TextPart("answered")]),
    ]
    await world.store.write_guild(int(GUILD), history)
    world.guild_state.agent_runner.history = list(history)
    await _publish(world.redis, _command())

    await _consume_once(world)

    text = _prompt_text(world.guild_state.agent_runner.history)
    assert "kaizen" not in text and NIA_NOTE in text
    assert attribution.ATTRIBUTION_MARK in text  # the fold's note is marked


def test_marked_note_with_uid_tail_and_no_hits_is_clean():
    history = [
        *memory_note_pair("nia asked about tokio", attributed=True),
        ModelRequest(parts=[UserPromptPart(_uid_line("more tokio"))]),
    ]
    assert purge.proactive_history_is_clean(history, TARGET)
    # Unattributed watcher-era replies with no evidence of attribution: fold.
    assert not purge.proactive_history_is_clean(
        [ModelResponse(parts=[TextPart("kaizen is right")])], TARGET
    )


async def test_ordinary_compaction_marks_only_attributed_input():
    async def summarize(_old):
        return "summary"

    from smarter_dev.bot.proactive.agent import compact_agent_history

    old_style = [
        ModelRequest(
            parts=[UserPromptPart("[2026-09-01T10:00:00Z] [id=5] A·kaizen: hi")]
        ),
        ModelResponse(parts=[TextPart("x" * 50)]),
        ModelRequest(parts=[UserPromptPart(_uid_line("tail"))]),
    ]
    compacted = await compact_agent_history(
        old_style, token_limit=0, summarize=summarize, keep_messages=1
    )
    assert attribution.ATTRIBUTION_MARK not in _prompt_text(compacted[:1])

    attributed = [
        ModelRequest(parts=[UserPromptPart(_uid_line("hi"))]),
        ModelResponse(parts=[TextPart("x" * 50)]),
        ModelRequest(parts=[UserPromptPart(_uid_line("tail"))]),
    ]
    compacted = await compact_agent_history(
        attributed, token_limit=0, summarize=summarize, keep_messages=1
    )
    assert attribution.ATTRIBUTION_MARK in _prompt_text(compacted[:1])


async def test_new_run_reinspects_a_store_done_by_an_earlier_run(world):
    await _publish(world.redis, _command())
    await _consume_once(world)
    # kai's id comes back into the guild history after the first run.
    regrown = [
        *world.guild_state.agent_runner.history,
        ModelRequest(parts=[UserPromptPart(f"kai ({KAI}) is back")]),
    ]
    world.guild_state.agent_runner.history = regrown
    await world.store.write_guild(int(GUILD), regrown)
    before = _calls(world, PROACTIVE_MARK)

    await _publish(world.redis, _command())
    await _consume_once(world)

    assert _calls(world, PROACTIVE_MARK) > before
    _assert_clean(_prompt_text(world.guild_state.agent_runner.history))


async def test_unreadable_proactive_history_is_failed_and_untouched(world):
    world.guild_state.history_loaded = False
    key = f"proactive:guild-history:{GUILD}"
    await world.redis.set(key, b"{garbage kai")
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.get(key) == b"{garbage kai"
    ack = world.acks[0][1]
    assert ack.outcome == "failed"
    assert "proactive guild history failed=1" in ack.detail


# -- 5. pending queue ----------------------------------------------------------


async def test_queue_before_purge_discarded_later_and_other_guilds_kept(world):
    other = world.run.guild_state_for(int(OTHER_GUILD))
    other.queue.push(
        Notification(kind="mention", created_at=_now(), body="Will asked")
    )
    world.guild_state.queue.push(
        Notification(kind="mention", created_at=_now(), body="nickname-only text")
    )
    await _publish(world.redis, _command(names=["Will"]))

    await _consume_once(world)
    world.guild_state.queue.push(
        Notification(kind="mention", created_at=_now(), body="Will said hi")
    )

    assert [n.body for n in world.guild_state.queue.items] == ["Will said hi"]
    assert [n.body for n in other.queue.items] == ["Will asked"]


# -- 6. unplaced channels per store kind ---------------------------------------


async def test_unplaced_channel_gets_chat_and_legacy_purged_and_reported(world):
    await world.store.write(UNPLACED_CHANNEL, [
        ModelRequest(parts=[UserPromptPart(f"kai ({KAI}) legacy")]),
        ModelResponse(parts=[TextPart("ok")]),
    ])
    await _publish(world.redis, _command())

    await _consume_once(world)

    _assert_clean(_prompt_text(await world.memory.read_history(UNPLACED_CHANNEL)))
    _assert_clean(_prompt_text(await world.store.read(UNPLACED_CHANNEL)))
    detail = world.acks[0][1].detail
    assert "chat channels purged=3" in detail
    assert "legacy channel histories purged=2" in detail


async def test_unplaced_legacy_key_is_not_written_when_owner_may_be_external(
    world,
):
    world.run.external_guild_ids = {OTHER_GUILD}
    raw = [
        ModelRequest(parts=[UserPromptPart(f"kai ({KAI}) legacy")]),
        ModelResponse(parts=[TextPart("ok")]),
    ]
    await world.store.write(UNPLACED_CHANNEL, raw)
    before = await world.redis.get(f"proactive:{UNPLACED_CHANNEL}:history")
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.get(f"proactive:{UNPLACED_CHANNEL}:history") == before
    ack = world.acks[0][1]
    assert ack.outcome == "failed" and "owner_unknown=1" in ack.detail


# -- 7. engines drop the guild memory they held --------------------------------


async def test_purge_makes_engines_reload_guild_memory(world, monkeypatch):
    from smarter_dev.bot.services import chat_engine as chat_engine_module
    from smarter_dev.bot.services.chat_engine import ChannelEngine
    from smarter_dev.bot.services.guild_chat_memory_service import GuildMemorySnapshot

    engine = ChannelEngine(
        bot=SimpleNamespace(), channel_id=LIVE_CHANNEL, guild_id=int(GUILD),
        voice_send=None, on_deactivate=None,
    )
    engine._long_term_memory = "kai (old blob) loves rust"
    engine._behavior = "be nice to kai"
    world.registry[LIVE_CHANNEL] = engine
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert engine._long_term_memory is None and engine._behavior is None

    # The next follow-up turn re-reads the purged memory and re-emits it.
    fresh = GuildMemorySnapshot(
        long_term_memory="nia likes tokio", updated_at=None, notes=(),
        behavior="be kind", personality=None,
    )
    engine._load_guild_memory = AsyncMock(return_value=fresh)
    builder = AsyncMock(side_effect=RuntimeError("stop after input"))
    monkeypatch.setattr(chat_engine_module, "build_followup_input", builder)
    memory = MagicMock()
    memory.read_history_versioned = AsyncMock(return_value=([], None))
    monkeypatch.setattr(chat_engine_module, "get_chat_memory", lambda: memory)
    monkeypatch.setattr(engine, "_get_channel_override", AsyncMock(return_value=None))
    monkeypatch.setattr(engine, "_budget_redis", lambda: None)
    monkeypatch.setattr(engine, "_post_error", AsyncMock())
    engine.queue.append(SimpleNamespace(message=SimpleNamespace(
        id=5, author=SimpleNamespace(id=int(NIA), is_bot=False))))

    await engine._run_once(first_activation=False)

    kwargs = builder.await_args.kwargs
    assert kwargs["long_term_memory"] == "nia likes tokio"
    assert kwargs["behavior"] == "be kind"
    assert "kai" not in json.dumps(kwargs, default=str)


# -- 8. unreadable chat history is never deleted --------------------------------


async def test_unreadable_chat_history_is_kept_failed_and_not_done(world, caplog):
    key = f"chat_agent:{STORED_CHANNEL}:history"
    await world.redis.set(key, b'{"kai": "' + KAI.encode() + b'"')
    payload = _command()
    await _publish(world.redis, payload)

    await _consume_once(world)

    assert await world.redis.get(key) is not None
    ack = world.acks[0][1]
    assert ack.outcome == "failed"
    done = await world.redis.hget(
        purge.purge_done_key(payload["run_id"]), f"chat_agent:{STORED_CHANNEL}"
    )
    assert done is None
    assert KAI not in caplog.text and "kai" not in caplog.text.lower()
    assert "input_value" not in caplog.text  # no pydantic error text


# -- 9. guild events about blocked members -------------------------------------


async def test_events_about_blocked_members_are_dropped(monkeypatch):
    from smarter_dev.bot.services import chat_engine as chat_engine_module
    from smarter_dev.bot.services.chat_engine import ChannelEngine
    from smarter_dev.shared.guild_event_log import mod_action_event

    get_blocked_users().load(1, [KAI])
    events = [
        mod_action_event(
            {"action_type": "timeout", "target_user_id": KAI,
             "target_username": "kai"}, guild_id=GUILD,
        ),
        mod_action_event(
            {"action_type": "warn", "target_user_id": NIA,
             "target_username": "nia"}, guild_id=GUILD,
        ),
    ]
    monkeypatch.setattr(
        chat_engine_module, "read_window", AsyncMock(return_value=(events, 1.0))
    )
    engine = ChannelEngine(
        bot=SimpleNamespace(), channel_id=1, guild_id=int(GUILD),
        voice_send=None, on_deactivate=None,
    )
    monkeypatch.setattr(engine, "_event_log_redis", lambda: object())

    views = await engine._drain_guild_events()

    assert [v.target_username for v in views] == ["nia"]


# -- 10. a turn never overwrites a purge's rewrite ------------------------------


async def test_turn_history_write_is_compare_and_set():
    redis = fakeredis.aioredis.FakeRedis()
    memory = ChatMemory(redis)
    turn = [ModelRequest(parts=[UserPromptPart("turn")])]
    await memory.write_history(1, [ModelRequest(parts=[UserPromptPart("old")])])
    _loaded, raw = await memory.read_history_versioned(1)
    # A purge (here or in another process) rewrites it meanwhile.
    await redis.set("chat_agent:1:history", b"[]", keepttl=True)

    assert not await memory.write_history(1, turn, expected_raw=raw)
    assert await redis.get("chat_agent:1:history") == b"[]"

    _loaded, raw = await memory.read_history_versioned(1)
    assert await memory.write_history(1, turn, expected_raw=raw)
    assert 0 < await redis.ttl("chat_agent:1:history")
    # Absent when loaded: written only if still absent.
    assert await memory.write_history(2, turn, expected_raw=None)
    assert not await memory.write_history(2, turn, expected_raw=None)


# -- 11. mention redaction can't be bypassed ------------------------------------


def test_unclosed_mention_before_a_real_one_is_still_redacted():
    cache = BlockedUsersCache()
    cache.load(1, [KAI])
    assert redact_blocked_mentions(f"<@ oops <@{KAI}> <@!{KAI}>", cache) == (
        "<@ oops @[blocked user] @[blocked user]"
    )


# -- 12. topic / notes rewrites never resurrect an expired key ------------------


async def test_replace_topic_and_notes_skip_expired_keys():
    redis = fakeredis.aioredis.FakeRedis()
    memory = ChatMemory(redis)

    assert not await memory.replace_topic(1, "x")
    assert not await memory.replace_notes(1, "x")
    assert await redis.get("chat_agent:1:topic") is None
    assert await redis.get("chat_agent:1:notes") is None

    await memory.write_notes(1, "old")
    assert await memory.replace_notes(1, "new")
    assert 0 < await redis.ttl("chat_agent:1:notes")


# -- 13. a redelivered run refolds what it did not finish ------------------------


async def test_redelivery_refolds_a_channel_it_did_not_finish(world, monkeypatch):
    """Run 1 writes LIVE_CHANNEL fully clean but dies before recording it
    done (and its ack post fails). The redelivered run folds that channel
    again although it now looks clean: not done means not trusted."""
    original_mark = purge._CommandScope.mark
    failed = []

    async def mark(self, store, value=purge._DONE):
        if store == f"chat_agent:{LIVE_CHANNEL}" and value == purge._DONE and not failed:
            failed.append(store)
            raise ConnectionError("redis blip")
        return await original_mark(self, store, value)

    monkeypatch.setattr(purge._CommandScope, "mark", mark)
    posts = []

    async def flaky_post(run_id, ack):
        posts.append(ack)
        if len(posts) == 1:
            raise ConnectionError("web down")
        world.acks.append((run_id, ack))
        return True

    world.deps.post_ack = flaky_post
    stream_id = await _publish(world.redis, _command())
    await _consume_once(world)
    assert await _pending(world.redis) == 1
    live = await world.memory.read_history(LIVE_CHANNEL)
    topic = (await world.memory.get_topic(LIVE_CHANNEL)).text
    notes = await world.memory.get_notes(LIVE_CHANNEL)
    target = PurgeTarget.build(KAI, ["kai", "Kai the Rustacean"])
    assert purge.chat_memory_is_clean(live, topic, notes, target)
    folds_before = _calls(world, CHAT_MARK)

    entries = await world.redis.xrange(PURGE_STREAM)
    await purge.process_entry(world.deps, stream_id, entries[0][1])

    # LIVE (forced) plus the two channels run 1 never reached.
    assert _calls(world, CHAT_MARK) == folds_before + 3
    assert world.acks[-1][1].outcome == "purged"


# -- 14. model calls time out ----------------------------------------------------


async def test_hung_model_fails_the_step_and_leaves_the_store(world, monkeypatch):
    monkeypatch.setattr(compaction, "MODEL_TIMEOUT_SECONDS", 0.05)
    calls = []

    async def hung(messages, info):
        calls.append(1)
        await asyncio.sleep(10)

    world.deps.chat_model = lambda: FunctionModel(hung)
    before = await world.redis.get(f"chat_agent:{LIVE_CHANNEL}:history")
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.get(f"chat_agent:{LIVE_CHANNEL}:history") == before
    assert world.acks[0][1].outcome == "failed"
    assert len(calls) == 3  # one attempt per chat channel, no retries


# -- 16. stream entries and unknown ownership -----------------------------------


async def test_malformed_entry_is_kept_while_the_worker_still_needs_it(world):
    await world.redis.xgroup_create(PURGE_STREAM, WORKER_CONSUMER_GROUP, id="0")
    await _publish(world.redis, "not json")

    await _consume_once(world)

    assert await world.redis.xlen(PURGE_STREAM) == 1  # worker has not read it
    assert await _pending(world.redis) == 0  # but the bot's group is done

    # Once the worker read and acked it, the next finisher deletes it.
    entries = await world.redis.xreadgroup(
        WORKER_CONSUMER_GROUP, "w", {PURGE_STREAM: ">"}
    )
    stream_id = entries[0][1][0][0]
    await world.redis.xack(PURGE_STREAM, WORKER_CONSUMER_GROUP, stream_id)
    await purge._finish_entry(world.redis, stream_id)
    assert await world.redis.xlen(PURGE_STREAM) == 0


async def test_unknown_run_is_deleted_even_if_the_worker_has_not_read_it(world):
    await world.redis.xgroup_create(PURGE_STREAM, WORKER_CONSUMER_GROUP, id="0")

    async def unknown(run_id, ack):
        return False

    world.deps.post_ack = unknown
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.xlen(PURGE_STREAM) == 0


async def test_missing_proactive_runtime_writes_no_proactive_store(world):
    world.deps.proactive = lambda: None
    world.guild_state.history_loaded = False
    key = f"proactive:guild-history:{GUILD}"
    before = await world.redis.get(key)
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.get(key) == before
    assert await world.redis.get(purge_epoch_key(GUILD)) is None
    ack = world.acks[0][1]
    assert ack.outcome == "failed" and "OwnershipUnknown" in ack.detail
    _assert_clean(_prompt_text(await world.memory.read_history(LIVE_CHANNEL)))


# -- convention: ack detail -------------------------------------------------------


async def test_ack_detail_reports_name_hits_per_step(world):
    world.summarizer.mode = "leak_name"
    await _publish(world.redis, _command())

    await _consume_once(world)

    detail = world.acks[0][1].detail
    segments = detail.split("; ")
    # Name-hit segments come first, one per step, before the counts.
    assert segments[:4] == [
        "chat_name_hits=3",
        "legacy_history_name_hits=1",
        "history_name_hits=1",
        "watch_instructions_name_hits=1",
    ]
    assert world.acks[0][1].outcome == "purged"
