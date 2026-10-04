"""Second project-agent review of PR 134 (at 05f2069f): one test per item."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.privacy import attribution
from smarter_dev.bot.privacy import purge
from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.privacy.blocked_users import redact_blocked_mentions
from smarter_dev.bot.privacy.plausibility import FoldField
from smarter_dev.bot.privacy.plausibility import fold_plausibility_problem
from smarter_dev.bot.privacy.plausibility import plausibility_rejection
from smarter_dev.bot.proactive.agent import memory_note_pair
from smarter_dev.bot.proactive.history_store import HistoryUnreadable
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.services.chat_memory import HISTORY_UNREADABLE
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import WORKER_CONSUMER_GROUP
from smarter_dev.shared.privacy_purge import PurgeTarget
from tests.bot.privacy_purge_test import GUILD
from tests.bot.privacy_purge_test import KAI
from tests.bot.privacy_purge_test import LIVE_CHANNEL
from tests.bot.privacy_purge_test import NIA
from tests.bot.privacy_purge_test import NIA_NOTES
from tests.bot.privacy_purge_test import NIA_SUMMARY
from tests.bot.privacy_purge_test import NIA_TOPIC
from tests.bot.privacy_purge_test import _assert_clean
from tests.bot.privacy_purge_test import _command
from tests.bot.privacy_purge_test import _consume_once
from tests.bot.privacy_purge_test import _prompt_text
from tests.bot.privacy_purge_test import _publish
from tests.bot.privacy_purge_test import _snapshot
from tests.bot.privacy_purge_test import build_world

TARGET = PurgeTarget.build(KAI, ["kai"])
OMAR = "333333333333333334"
LIN = "333333333333333335"


@pytest.fixture
async def world():
    return await build_world()


def _line(display: str, uid: str, text: str, msg: int = 9) -> str:
    return f"[2026-10-03T10:00:00Z] [id={msg}] A·{display} (uid={uid}): {text}"


def _chat_model(payload: dict):
    def respond(messages, info: AgentInfo) -> ModelResponse:
        text = _prompt_text(messages)
        if "rewrite a Discord chat agent's working memory" in text:
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, payload)]
            )
        return ModelResponse(parts=[TextPart(f"nia (uid={NIA}) asked about tokio.")])

    return FunctionModel(respond)


# -- M1 fold plausibility -------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": ".", "topic": NIA_TOPIC, "notes": NIA_NOTES},
        {
            "summary": "I'm sorry, but I cannot help with rewriting this memory.",
            "topic": NIA_TOPIC,
            "notes": NIA_NOTES,
        },
        {"summary": NIA_SUMMARY, "notes": NIA_NOTES},  # topic missing
        {"summary": NIA_SUMMARY, "topic": NIA_TOPIC},  # notes missing
        {"summary": NIA_SUMMARY, "topic": "", "notes": NIA_NOTES},
    ],
    ids=["lazy", "refusal", "no-topic", "no-notes", "blank-topic"],
)
async def test_implausible_chat_fold_fails_and_leaves_every_store(world, payload):
    # Bystander text in the topic and notes, so they may not come back empty.
    for channel in (LIVE_CHANNEL,):
        await world.memory.write_topic(channel, f"kai ({KAI}) was here.\n{NIA_TOPIC}")
        await world.memory.write_notes(channel, f"{NIA_NOTES}\nkai ({KAI}) too")
    world.deps.chat_model = lambda: _chat_model(payload)
    before = await _snapshot(world.redis)
    await _publish(world.redis, _command(guild_ids=[GUILD]))

    await _consume_once(world)

    after = await _snapshot(world.redis)
    # The channel whose topic and notes carry bystander text: untouched.
    for key in before:
        if key.startswith(f"chat_agent:{LIVE_CHANNEL}:".encode()):
            assert after[key] == before[key], key
    ack = world.acks[0][1]
    assert ack.outcome == "failed"


async def test_proactive_fold_that_drops_bystanders_fails_untouched(world):
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    "\n".join(
                        [
                            _line("nia", NIA, "tokio benchmarks anyone?", 1),
                            _line("omar", OMAR, "I have numbers for async-std", 2),
                            _line("lin", LIN, "criterion is the way", 3),
                            _line("kai", KAI, "mine are secret", 4),
                        ]
                    )
                )
            ]
        ),
        ModelResponse(parts=[TextPart("noted")]),
    ]
    await world.store.write_guild(int(GUILD), history)
    world.guild_state.agent_runner.history = list(history)
    raw_before = await world.redis.get(f"proactive:guild-history:{GUILD}")
    calls = []

    def forgetful(messages, info):
        calls.append(1)
        return ModelResponse(
            parts=[TextPart("Someone asked about benchmarks; it was discussed at length.")]
        )

    world.deps.proactive_model = lambda: FunctionModel(forgetful)
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.get(f"proactive:guild-history:{GUILD}") == raw_before
    assert world.guild_state.agent_runner.history == history
    assert "proactive guild history failed=1" in world.acks[0][1].detail
    assert len(calls) >= 3  # first try plus two retries


def test_plausibility_rule_matches_the_worker_vectors():
    inputs = [_line("nia", NIA, "x" * 2000), _line("kai", KAI, "secret")]
    # Length floor: ceil(0.05 * len(B)) when len(B) >= 200.
    assert "far shorter" in fold_plausibility_problem(
        inputs, TARGET, f"nia (uid={NIA}) said a lot about many things."
    )
    long_enough = f"nia (uid={NIA}) " + "kept memory " * 10
    assert fold_plausibility_problem(inputs, TARGET, long_enough) is None
    # Retention by display name beside the uid.
    assert fold_plausibility_problem(
        inputs, TARGET, "nia " + "kept memory of the talk " * 5
    ) is None
    # Input wholly about the target: an empty or short output is allowed...
    only_kai = [_line("kai", KAI, "secret")]
    assert fold_plausibility_problem(only_kai, TARGET, "") is None
    assert fold_plausibility_problem(only_kai, TARGET, "ok") is None
    # ...but never a refusal, and an empty input needs an empty output.
    assert fold_plausibility_problem(only_kai, TARGET, "I cannot.") is not None
    assert fold_plausibility_problem([""], TARGET, "anything") is not None
    # The bot's extra: a missing field is rejected when its input existed.
    assert plausibility_rejection(
        [FoldField("topic", ("a topic about tokio",), None)], TARGET
    )


# -- M2 unreadable chat history on the turn path ----------------------------------


async def test_unreadable_chat_history_is_never_deleted_or_overwritten(caplog):
    redis = fakeredis.aioredis.FakeRedis()
    memory = ChatMemory(redis)
    garbage = b'{"kai": "' + KAI.encode() + b'"'
    await redis.set("chat_agent:1:history", garbage, ex=600)

    history, version = await memory.read_history_versioned(1)
    assert history == [] and version is HISTORY_UNREADABLE
    assert await memory.read_history(1) == []
    # The turn's write (either kind) is refused while it is unreadable.
    turn = [ModelRequest(parts=[UserPromptPart("turn")])]
    assert not await memory.write_history(1, turn, expected_raw=version)

    assert await redis.get("chat_agent:1:history") == garbage
    assert KAI not in caplog.text and "input_value" not in caplog.text


async def test_unreadable_topic_and_notes_are_left_alone():
    redis = fakeredis.aioredis.FakeRedis()
    memory = ChatMemory(redis)
    await redis.set("chat_agent:1:topic", b"\xff\xfe", ex=600)
    await redis.set("chat_agent:1:topic_ts", datetime.now(UTC).isoformat())
    await redis.set("chat_agent:1:notes", b"\xff\xfe", ex=600)

    assert await memory.get_topic(1) is None
    assert await memory.get_notes(1) is None
    await memory.write_topic(1, "new")
    await memory.write_notes(1, "new")

    assert await redis.get("chat_agent:1:topic") == b"\xff\xfe"
    assert await redis.get("chat_agent:1:notes") == b"\xff\xfe"


# -- M3 unreadable proactive history ----------------------------------------------


async def test_unreadable_stored_bytes_fail_even_when_ram_is_loaded(world):
    key = f"proactive:guild-history:{GUILD}"
    await world.redis.set(key, b"{garbage kai")
    assert world.guild_state.history_loaded  # RAM holds a readable copy
    payload = _command()
    await _publish(world.redis, payload)

    await _consume_once(world)

    assert await world.redis.get(key) == b"{garbage kai"
    assert "proactive guild history failed=1" in world.acks[0][1].detail
    assert not await world.redis.hget(
        purge.purge_done_key(payload["run_id"]), f"proactive:guild-history:{GUILD}"
    )
    # The runtime re-reads before its next wake rather than writing RAM back.
    assert world.guild_state.history_loaded is False


async def test_read_guild_signals_unreadable_distinctly():
    redis = fakeredis.aioredis.FakeRedis()
    store = ProactiveHistoryStore(redis)
    assert await store.read_guild(2) == []
    await redis.set("proactive:guild-history:2", b"not json")
    with pytest.raises(HistoryUnreadable) as error:
        await store.read_guild(2)
    assert "not json" not in str(error.value)


async def test_wake_never_overwrites_an_unreadable_history(monkeypatch):
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.proactive.agent import KimiAgentRunner
    from smarter_dev.bot.proactive.agent import build_kimi_agent
    from smarter_dev.bot.proactive.notifications import Notification
    from tests.bot.privacy_model_input_test import _fake_bot
    from tests.bot.privacy_model_input_test import _Settings

    redis = fakeredis.aioredis.FakeRedis()
    await redis.set("proactive:guild-history:2", b"{unreadable")
    monkeypatch.setenv(proactive.WATCHER_MODEL_ENV_VAR, "z-ai/glm-5.3-flash")
    bot = _fake_bot(
        [], d={"proactive_settings_service": _Settings(), "chat_memory_redis": redis}
    )
    run = proactive.ProactiveRuntime(bot, start_consumers=False)
    run._agent_model_id = "test"
    monkeypatch.setattr(proactive, "runtime", run)
    monkeypatch.setattr(run, "skim", lambda: SimpleNamespace())
    seen = []

    def model(messages, info):
        seen.append(len(messages))
        return ModelResponse(parts=[TextPart("quiet")])

    run.agent_runner_for = lambda state: state.agent_runner or setattr(
        state,
        "agent_runner",
        KimiAgentRunner(
            agent=build_kimi_agent(FunctionModel(model), system_prompt="t"),
            summarize=AsyncMock(),
        ),
    ) or state.agent_runner
    state = run.guild_state_for(2)
    state.queue.push(
        Notification(kind="mention", created_at=datetime.now(UTC), body="hi", wakes=True)
    )

    await proactive._consume_guild_once(state)

    assert seen  # the wake ran
    assert await redis.get("proactive:guild-history:2") == b"{unreadable"
    assert state.history_loaded is False


# -- M4 guild events --------------------------------------------------------------


async def test_events_by_or_about_blocked_members_are_dropped(monkeypatch):
    from smarter_dev.bot.services import chat_engine as chat_engine_module
    from smarter_dev.bot.services.chat_engine import ChannelEngine
    from smarter_dev.shared.guild_event_log import BOT_DM_KIND
    from smarter_dev.shared.guild_event_log import bot_message_event
    from smarter_dev.shared.guild_event_log import mod_action_event

    get_blocked_users().load(1, [KAI])
    events = [
        bot_message_event(
            guild_id=GUILD, summary="a handler message", kind=BOT_DM_KIND,
            target_user_id=KAI,
        ),
        mod_action_event(
            {"action_type": "warn", "target_user_id": NIA,
             "moderator_user_id": KAI, "moderator_username": "kai"},
            guild_id=GUILD,
        ),
        mod_action_event(  # the old handler shape: the id as the username
            {"action_type": "ban", "target_username": KAI}, guild_id=GUILD
        ),
        bot_message_event(guild_id=GUILD, summary=f"welcomed user {KAI}"),
        mod_action_event(
            {"action_type": "warn", "target_user_id": NIA,
             "target_username": "nia"},
            guild_id=GUILD,
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


async def test_handler_dm_and_admin_actions_record_the_member_id(monkeypatch):
    from smarter_dev.web import admin_actions
    from smarter_dev.web import handler_emitter

    recorded = []

    async def record(event):
        recorded.append(event)

    monkeypatch.setattr(handler_emitter, "record_guild_event", record)
    monkeypatch.setattr(admin_actions, "record_guild_event", record)

    emitter = object.__new__(handler_emitter.DiscordEmitter)
    emitter.guild_id = GUILD
    emitter._resolve_dm_channel = AsyncMock(return_value="555")
    emitter._request = AsyncMock(
        return_value=SimpleNamespace(json=lambda: {"id": "1"})
    )
    await emitter.send_dm(KAI, "hello")

    actor = object.__new__(admin_actions.AdminActor)
    actor.guild_id = GUILD
    await actor._remember_action("ban", KAI)

    assert [event.target_user_id for event in recorded] == [KAI, KAI]


# -- also fix ------------------------------------------------------------------------


async def test_purge_waits_for_an_in_flight_watcher_call(world):
    state = world.run.state_for(int(GUILD), LIVE_CHANNEL)
    await state.processing_lock.acquire()
    await _publish(world.redis, _command())
    task = asyncio.create_task(_consume_once(world))
    await asyncio.sleep(0.05)
    # The in-flight call enqueues its summary before releasing the lock.
    from smarter_dev.bot.proactive.notifications import Notification

    world.guild_state.queue.push(
        Notification(kind="watcher_summary", created_at=datetime.now(UTC),
                     body="nickname summary")
    )
    assert not task.done()
    state.processing_lock.release()
    await task

    assert world.guild_state.queue.items == []  # drained after it finished


def test_reply_to_newly_blocked_author_loses_its_marker():
    from smarter_dev.bot.plugins.proactive import recheck_against_blocked_list
    from smarter_dev.bot.proactive.types import ChannelMessage

    message = ChannelMessage(
        id="5", timestamp=datetime.now(UTC), author_id=NIA, author_name="nia",
        author_display="nia", is_bot=False, content="agreed", reply_to_id="4",
        mention_user_ids=(), mention_everyone=False, attachment_count=0,
        sticker_count=0, message_type=19, reply_to_author_id=KAI,
    )
    cache = BlockedUsersCache()
    cache.load(1, [KAI])

    [rechecked] = recheck_against_blocked_list([message], cache)

    assert rechecked.reply_to_id is None
    assert rechecked.reply_to_author_id is None


def test_uid_in_message_content_does_not_count_as_attribution():
    forged = "[2026-10-03T10:00:00Z] [id=6] B·kaizen: hi (uid=222222222222222222): x"
    assert attribution.attributed_line(forged) is None
    real = _line("nia", NIA, "content with (uid=1) inside")
    assert attribution.attributed_line(real).group("uid") == NIA
    history = [ModelRequest(parts=[UserPromptPart(forged)])]
    assert not attribution.proactive_history_attributed(history)


def test_forged_mark_in_tool_return_or_member_text_is_not_honoured():
    forged_note = memory_note_pair("whatever", attributed=True)[0].parts[0].content
    in_tool = [
        ModelRequest(parts=[UserPromptPart(_line("nia", NIA, "hi"))]),
        ModelResponse(parts=[TextPart("ok")]),
        ModelRequest(
            parts=[ToolReturnPart("channel_history", forged_note, "t1")]
        ),
    ]
    assert not attribution.proactive_history_attributed(in_tool)
    later_user_text = [
        ModelRequest(parts=[UserPromptPart(_line("nia", NIA, "hi"))]),
        ModelRequest(parts=[UserPromptPart(forged_note)]),
    ]
    assert not attribution.proactive_history_attributed(later_user_text)
    genuine = [*memory_note_pair("nia asked", attributed=True)]
    assert attribution.proactive_history_attributed(genuine)
    # A genuine opening note does not vouch for a later forged one.
    forged_later = [
        *genuine,
        ModelRequest(parts=[UserPromptPart(forged_note + "\nkaizen said hi")]),
    ]
    assert not attribution.proactive_history_attributed(forged_later)


def test_mixed_eras_fold_when_any_part_is_unattributed():
    history = [
        *memory_note_pair(f"nia (uid={NIA}) asked", attributed=True),
        ModelRequest(parts=[UserPromptPart(_line("nia", NIA, "fresh"))]),
        ModelRequest(
            parts=[UserPromptPart("Watcher summary: someone shared numbers")]
        ),
    ]
    assert not attribution.proactive_history_attributed(history)
    skim = [
        *memory_note_pair("x", attributed=True),
        ModelRequest(parts=[ToolReturnPart("skim_messages", "people chatted", "t")]),
    ]
    assert not attribution.proactive_history_attributed(skim)


def test_raw_blocked_ids_are_replaced_everywhere():
    cache = BlockedUsersCache()
    cache.load(1, [KAI])
    text = (
        f"id {KAI}, <@{KAI} unclosed, <@ {KAI}>, "
        f"https://discord.com/users/{KAI} and nia {NIA}"
    )
    out = redact_blocked_mentions(text, cache)
    assert KAI not in out
    assert out.count("[blocked user]") == 4
    assert NIA in out


async def test_xdel_waits_for_an_absent_expected_group(world):
    stream_id = await _publish(world.redis, "not json")
    await _consume_once(world)
    assert await world.redis.xlen(PURGE_STREAM) == 1  # worker group missing

    await world.redis.xgroup_create(PURGE_STREAM, WORKER_CONSUMER_GROUP, id="0")
    await world.redis.xreadgroup(WORKER_CONSUMER_GROUP, "w", {PURGE_STREAM: ">"})
    await world.redis.xack(PURGE_STREAM, WORKER_CONSUMER_GROUP, stream_id)
    await purge._finish_entry(world.redis, stream_id)
    assert await world.redis.xlen(PURGE_STREAM) == 0


async def test_topic_without_timestamp_is_still_purged(world):
    await world.redis.delete(f"chat_agent:{LIVE_CHANNEL}:topic_ts")
    await _publish(world.redis, _command())

    await _consume_once(world)

    topic = await world.redis.get(f"chat_agent:{LIVE_CHANNEL}:topic")
    _assert_clean(topic.decode())


async def test_watch_instructions_without_the_person_skip_the_model(world):
    from smarter_dev.bot.proactive.environment import InstructionStore
    from smarter_dev.bot.proactive.environment import WatchInstruction

    expires = datetime.now(UTC).replace(year=2099)
    clean = InstructionStore(
        seed="s", entries=[WatchInstruction("w1", "wake for tokio news", expires)]
    )
    world.settings.stored = clean.to_stored()
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert world.settings.saved == []
    assert not [c for c in world.summarizer.calls if "watch instructions" in c]
    assert "watch instructions unchanged=1" in world.acks[0][1].detail


async def test_unchecked_names_are_reported_first(world):
    await _publish(world.redis, _command(names=["k", "kai"]))

    await _consume_once(world)

    assert world.acks[0][1].detail.startswith("unchecked_names=1; ")


# -- chat retention (bot-only, input-format extension) ----------------------------


def _chat_message(uid: str, name: str, body: str, msg: int) -> str:
    return (
        f'<message id="{msg}" user-id="{uid}" username="{name}">\n{body}\n</message>'
    )


async def _chat_world_with_bystanders(world):
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(_chat_message(NIA, "nia", "tokio benchmarks?", 1)),
            ]
        ),
        ModelResponse(parts=[TextPart("use criterion")]),
        ModelRequest(
            parts=[UserPromptPart(_chat_message(OMAR, "omar", "async-std too", 2))]
        ),
        ModelRequest(
            parts=[UserPromptPart(_chat_message(LIN, "lin", "flamegraphs!", 3))]
        ),
        ModelRequest(
            parts=[UserPromptPart(_chat_message(KAI, "kai", "my secret", 4))]
        ),
        ModelResponse(parts=[TextPart("thanks all")]),
    ]
    for channel in (10, 11, 12, 13):
        await world.redis.delete(
            f"chat_agent:{channel}:topic", f"chat_agent:{channel}:notes"
        )
    await world.memory.write_history(LIVE_CHANNEL, history)
    return history


async def test_chat_fold_that_drops_every_bystander_fails_untouched(world):
    await _chat_world_with_bystanders(world)
    raw_before = await world.redis.get(f"chat_agent:{LIVE_CHANNEL}:history")
    summary = (
        "Participants: several people. They discussed runtime benchmarking "
        "tools at length and settled on profiling approaches."
    )
    world.deps.chat_model = lambda: _chat_model(
        {"summary": summary, "topic": "", "notes": ""}
    )
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert await world.redis.get(f"chat_agent:{LIVE_CHANNEL}:history") == raw_before
    assert world.acks[0][1].outcome == "failed"


async def test_chat_fold_that_keeps_half_the_bystanders_passes(world):
    await _chat_world_with_bystanders(world)
    summary = (
        f"Participants: nia (uid={NIA}), omar (user-id={OMAR}). nia asked about "
        "tokio benchmarks; omar added async-std."
    )
    world.deps.chat_model = lambda: _chat_model(
        {"summary": summary, "topic": "", "notes": ""}
    )
    await _publish(world.redis, _command())

    await _consume_once(world)

    text = _prompt_text(await world.memory.read_history(LIVE_CHANNEL))
    assert "omar added async-std" in text
    _assert_clean(text)


def test_chat_retention_counts_display_names_and_user_id_forms():
    from smarter_dev.bot.privacy.plausibility import fold_plausibility_problem

    inputs = [
        _chat_message(NIA, "nia", "tokio?", 1),
        _chat_message(OMAR, "omar", "async-std", 2),
        _chat_message(LIN, "lin &amp; co", "flamegraphs", 3),
        _chat_message(KAI, "kai", "secret", 4),
    ]
    filler = " talked about runtimes and profiling in some detail."
    assert fold_plausibility_problem(inputs, TARGET, "nia and omar" + filler) is None
    assert fold_plausibility_problem(
        inputs, TARGET, f"user-id={NIA} and lin & co" + filler
    ) is None
    assert "1 of the 3 other chat participants" in fold_plausibility_problem(
        inputs, TARGET, "omar" + filler
    )
