"""A blocked author's Discord messages never reach the chat or proactive model.

Each test captures what a pydantic-ai FunctionModel (or the watcher) actually
receives — prompts, history and tool returns — and checks it for the blocked
user's id, name, words and files. Synthetic users only: kai (blocked) and nia.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.agents import chat_context
from smarter_dev.bot.agents.chat_input_format import build_agent_call
from smarter_dev.bot.plugins import mention
from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.proactive.adapter import WatcherProducer
from smarter_dev.bot.proactive.agent import KimiAgentRunner
from smarter_dev.bot.proactive.agent import build_kimi_agent
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.notifications import NotificationQueue
from smarter_dev.bot.proactive.types import ActivationContext
from smarter_dev.bot.proactive.watcher import WatcherDecision
from smarter_dev.bot.services.proactive_settings_service import EnabledProactiveChannel
from smarter_dev.bot.services.proactive_settings_service import ProactiveChannelSettings
from smarter_dev.shared.privacy_purge import BLOCKED_PLACEHOLDER

KAI = 111111111111111111
NIA = 222222222222222222
BOT = 999
GUILD = 2
CHANNEL = 1
KAI_WORDS = "kai's secret rust benchmark"
KAI_FILE = "https://cdn.discordapp.com/attachments/1/2/kai-private.png"
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _user(user_id, name, *, is_bot=False):
    return SimpleNamespace(
        id=user_id, username=name, global_name=name.title(), is_bot=is_bot
    )


def _message(
    message_id,
    author,
    content,
    *,
    minutes=0,
    reply_to=None,
    mentions=(),
    attachments=(),
):
    return SimpleNamespace(
        id=message_id,
        created_at=T0 + timedelta(minutes=minutes),
        timestamp=T0 + timedelta(minutes=minutes),
        author=author,
        member=SimpleNamespace(
            nickname=None, get_roles=lambda: [SimpleNamespace(name="Regular")]
        ),
        content=content,
        type=0,
        referenced_message=reply_to,
        user_mentions_ids=tuple(mentions),
        mentions_everyone=False,
        attachments=list(attachments),
        stickers=[],
        reactions=[],
    )


KAI_USER = _user(KAI, "kai")
NIA_USER = _user(NIA, "nia")
BOT_USER = _user(BOT, "smarter-bot", is_bot=True)
KAI_ATTACHMENT = SimpleNamespace(
    url=KAI_FILE, filename="kai-private.png", media_type="image/png", size=10
)


def _channel_messages():
    """Oldest first: nia, kai (with a file), nia replying to kai and @-ing him,
    and the bot answering nia."""
    nia_hello = _message(1001, NIA_USER, "anyone benchmarked tokio?", minutes=0)
    kai_post = _message(
        1002,
        KAI_USER,
        KAI_WORDS,
        minutes=1,
        attachments=[KAI_ATTACHMENT],
        mentions=(NIA,),
    )
    nia_reply = _message(
        1003,
        NIA_USER,
        f"<@{KAI}> nice numbers, nia agrees",
        minutes=2,
        reply_to=kai_post,
        mentions=(KAI,),
    )
    bot_reply = _message(1004, BOT_USER, "glad it helped", minutes=3)
    return [nia_hello, kai_post, nia_reply, bot_reply]


def _assert_no_trace(text: str) -> None:
    assert str(KAI) not in text
    assert "kai" not in text.lower()
    assert "1002" not in text  # the blocked message's id
    assert "kai-private" not in text


class _Iterator:
    def __init__(self, messages):
        self._messages = list(messages)

    def limit(self, n):
        return _Iterator(self._messages[:n])

    def __aiter__(self):
        async def generate():
            for message in self._messages:
                yield message

        return generate()


def _fake_bot(messages, *, d=None):
    def fetch_messages(channel_id, before=None, after=None):
        pool = list(reversed(messages))  # Discord returns newest first
        if before is not None:
            pool = [m for m in pool if int(m.id) < int(before)]
        if after is not None:
            pool = [m for m in pool if int(m.id) > int(after)]
        return _Iterator(pool)

    async def fetch_member(guild_id, user_id):
        name = {KAI: "kai", NIA: "nia", BOT: "smarter-bot"}[int(user_id)]
        return SimpleNamespace(nickname=f"{name}-nick", role_ids=[])

    rest = SimpleNamespace(
        fetch_messages=fetch_messages,
        fetch_roles=AsyncMock(return_value=[]),
        fetch_member=fetch_member,
        fetch_role=AsyncMock(return_value=None),
        create_message=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    return SimpleNamespace(
        rest=rest,
        d=d if d is not None else {},
        get_me=lambda: BOT_USER,
        cache=SimpleNamespace(
            get_guild_channel=lambda cid: SimpleNamespace(name="general"),
            get_guild=lambda gid: SimpleNamespace(name="Smarter Dev"),
            get_guilds_view=lambda: {GUILD: SimpleNamespace(id=GUILD)},
            get_message=lambda mid: None,
        ),
    )


@pytest.fixture
def kai_blocked():
    blocked = get_blocked_users()
    blocked.load(5, [str(KAI)])
    return blocked


def _capturing_model(responses=None):
    """A FunctionModel recording every message list it was handed."""
    seen: list[str] = []
    script = list(responses or [])

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen.append(ModelMessagesTypeAdapter.dump_json(messages).decode())
        if script:
            return script.pop(0)
        return ModelResponse(parts=[TextPart("nothing to do")])

    return FunctionModel(respond), seen


# -- chat agent ---------------------------------------------------------------


class _Memory:
    async def topic_for_activation(self, channel_id):
        return None

    async def get_notes(self, channel_id):
        return None


@pytest.fixture
def chat_bot(monkeypatch):
    messages = _channel_messages()
    monkeypatch.setattr(
        chat_context,
        "fetch_channel_info",
        AsyncMock(return_value={"channel_name": "general"}),
    )
    return messages


async def test_chat_initial_input_renders_blocked_message_as_placeholder(
    kai_blocked, chat_bot
):
    messages = chat_bot
    trigger = _message(1005, NIA_USER, "<@999> what did we decide?", minutes=4,
                       mentions=(BOT,))
    bot = _fake_bot([*messages, trigger])

    agent_input = await chat_context.build_initial_input(
        bot=bot,
        channel_id=CHANNEL,
        guild_id=GUILD,
        memory=_Memory(),
        trigger_message=trigger,
    )
    prompt, history = build_agent_call(agent_input, [])
    model, seen = _capturing_model()
    await Agent(model).run(prompt, message_history=history)

    received = "\n".join(seen)
    assert BLOCKED_PLACEHOLDER in received
    _assert_no_trace(received)
    # nia's words and her (redacted) mention of kai survive.
    assert "anyone benchmarked tokio?" in received
    assert "@[blocked user] nice numbers" in received
    assert str(NIA) in received
    assert [author.user_id for author in agent_input.authors] == [str(NIA), str(BOT)]


async def test_chat_followup_turns_kai_into_placeholder_even_if_queued(
    kai_blocked, chat_bot
):
    bot = _fake_bot(chat_bot)
    queued = [chat_bot[1], chat_bot[2]]

    agent_input = await chat_context.build_followup_input(
        bot=bot, channel_id=CHANNEL, guild_id=GUILD, queued=queued, memory=_Memory()
    )
    prompt, history = build_agent_call(agent_input, [])
    model, seen = _capturing_model()
    await Agent(model).run(prompt, message_history=history)

    received = "\n".join(seen)
    assert received.count(BLOCKED_PLACEHOLDER) == 1
    _assert_no_trace(received)


async def test_blocked_author_engaging_the_bot_gets_no_model_call(
    kai_blocked, monkeypatch
):
    registry_getter = MagicMock()
    monkeypatch.setattr(mention, "get_chat_engine_registry", registry_getter)
    fake_bot = SimpleNamespace(get_me=MagicMock(return_value=None))
    monkeypatch.setattr(mention, "plugin", SimpleNamespace(bot=fake_bot))
    kai_mention = _message(1006, KAI_USER, "<@999> hi", mentions=(BOT,))
    event = SimpleNamespace(
        message=kai_mention, guild_id=GUILD, channel_id=CHANNEL, content="hi"
    )

    await mention.on_message_create(event)

    registry_getter.assert_not_called()
    # Only the opted-out notice looks up the bot user (#92), and stops there.
    calls = fake_bot.get_me.call_count

    # nia passes the gate (and stops at the stubbed bot user).
    event.message = _message(1007, NIA_USER, "<@999> hi", mentions=(BOT,))
    await mention.on_message_create(event)
    assert fake_bot.get_me.call_count == calls + 1


async def test_chat_cold_start_routes_nobody_to_the_engine(monkeypatch):
    get_blocked_users().reset()
    registry_getter = MagicMock()
    monkeypatch.setattr(mention, "get_chat_engine_registry", registry_getter)
    fake_bot = SimpleNamespace(get_me=MagicMock(return_value=BOT_USER))
    monkeypatch.setattr(mention, "plugin", SimpleNamespace(bot=fake_bot))
    event = SimpleNamespace(
        message=_message(1007, NIA_USER, "<@999> hi", mentions=(BOT,)),
        guild_id=GUILD,
        channel_id=CHANNEL,
        content="hi",
    )

    await mention.on_message_create(event)

    registry_getter.assert_not_called()
    fake_bot.get_me.assert_not_called()


async def test_chat_cold_start_converter_sends_no_discord_text(chat_bot):
    get_blocked_users().reset()
    bot = _fake_bot(chat_bot)

    agent_input = await chat_context.build_followup_input(
        bot=bot, channel_id=CHANNEL, guild_id=GUILD, queued=chat_bot,
        memory=_Memory(),
    )
    prompt, history = build_agent_call(agent_input, [])

    rendered = prompt + ModelMessagesTypeAdapter.dump_json(history).decode()
    assert "tokio" not in rendered
    assert "glad it helped" not in rendered
    _assert_no_trace(rendered)


async def test_chat_engine_defers_turn_until_list_loads(monkeypatch):
    from smarter_dev.bot.services.chat_engine import ChannelEngine

    get_blocked_users().reset()
    engine = ChannelEngine(
        bot=SimpleNamespace(),
        channel_id=CHANNEL,
        guild_id=GUILD,
        voice_send=AsyncMock(),
        on_deactivate=AsyncMock(),
    )
    engine.activation_message = _message(1007, NIA_USER, "hi")
    memory_getter = MagicMock()
    monkeypatch.setattr(
        "smarter_dev.bot.services.chat_engine.get_chat_memory", memory_getter
    )

    consumed = await engine._run_once(first_activation=True)

    assert consumed is False
    memory_getter.assert_not_called()


async def test_response_gate_grounding_drops_blocked_messages(kai_blocked):
    from smarter_dev.bot.services.chat_engine import ChannelEngine

    messages = _channel_messages()
    engine = ChannelEngine(
        bot=_fake_bot(messages),
        channel_id=CHANNEL,
        guild_id=GUILD,
        voice_send=AsyncMock(),
        on_deactivate=AsyncMock(),
    )
    candidate = _message(1005, NIA_USER, f"<@{KAI}> right?", minutes=4)

    grounding = await engine._fetch_gate_grounding([candidate])
    gate_candidate = engine._to_gate_message(candidate)

    text = repr(grounding) + repr(gate_candidate)
    assert [g.message_id for g in grounding] == ["1001", "1003", "1004"]
    _assert_no_trace(text)


# -- proactive agent ----------------------------------------------------------


class _Settings:
    def __init__(self, addendum=""):
        self.addendum = addendum

    async def get_settings(self, guild_id, channel_id):
        return ProactiveChannelSettings(
            guild_id=str(GUILD), channel_id=str(CHANNEL), enabled=True,
            watch_addendum=self.addendum,
        )

    async def list_enabled_channels(self, guild_id):
        return [
            EnabledProactiveChannel(
                channel_id=str(CHANNEL), watch_addendum=self.addendum
            )
        ]

    async def set_watch_addendum(self, guild_id, channel_id, addendum):
        self.addendum = addendum

    async def record_wake_usage(self, *args, **kwargs):
        return None


@pytest.fixture
def proactive_run(monkeypatch):
    monkeypatch.setenv(proactive.WATCHER_MODEL_ENV_VAR, "z-ai/glm-5.3-flash")
    messages = _channel_messages()
    service = _Settings()
    bot = _fake_bot(messages, d={"proactive_settings_service": service})
    run = proactive.ProactiveRuntime(bot, start_consumers=False)
    run._agent_model_id = "test-agent"
    monkeypatch.setattr(proactive, "runtime", run)
    monkeypatch.setattr(run, "skim", lambda: SimpleNamespace())
    monkeypatch.setattr(proactive, "_schedule_producer", lambda state: None)
    return SimpleNamespace(run=run, bot=bot, messages=messages, service=service)


def _event(message):
    return SimpleNamespace(
        author=message.author, guild_id=GUILD, channel_id=CHANNEL, message=message
    )


async def test_proactive_live_ingest_drops_blocked_author(kai_blocked, proactive_run):
    kai_mention = _message(2001, KAI_USER, f"<@{BOT}> {KAI_WORDS}", mentions=(BOT,))

    await proactive.on_guild_message(_event(kai_mention))

    state = proactive_run.run.channel_states.get(CHANNEL)
    assert state is None or not state.buffer
    assert not proactive_run.run.guild_states  # no notification, no wake


async def test_proactive_reaction_by_blocked_user_is_dropped(
    kai_blocked, proactive_run
):
    event = SimpleNamespace(
        guild_id=GUILD, channel_id=CHANNEL, user_id=KAI, message_id=1004,
        member=SimpleNamespace(display_name="kai"), emoji_name="👍",
    )

    await proactive.on_guild_reaction(event)

    assert not proactive_run.run.channel_states
    assert not proactive_run.run.guild_states


async def test_proactive_agent_never_sees_kai_in_brief_history_or_tools(
    kai_blocked, proactive_run
):
    run = proactive_run.run
    responses = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    "channel_history", {"channel_id": str(CHANNEL), "limit": 20}
                ),
                ToolCallPart(
                    "lookup_message",
                    {"channel_id": str(CHANNEL), "message_id": "1002"},
                ),
                ToolCallPart(
                    "lookup_message", {"channel_id": str(CHANNEL), "message_id": ""}
                ),
            ]
        ),
        ModelResponse(parts=[TextPart("stayed quiet")]),
    ]
    model, seen = _capturing_model(responses)
    runner = KimiAgentRunner(
        agent=build_kimi_agent(model, system_prompt="test"),
        summarize=AsyncMock(),
    )
    monkeypatch_runner = lambda state: runner  # noqa: E731
    run.agent_runner_for = monkeypatch_runner

    nia_mention = _message(
        2002, NIA_USER, f"<@{BOT}> what did <@{KAI}> post?", mentions=(BOT, KAI),
        minutes=5,
    )
    await proactive.on_guild_message(_event(nia_mention))
    guild_state = run.guild_states[GUILD]
    assert guild_state.queue.items  # nia's mention woke the agent

    await proactive._consume_guild_once(guild_state)

    received = "\n".join(seen)
    assert len(seen) == 2
    assert BLOCKED_PLACEHOLDER in received  # channel_history tool return
    assert "No message with id 1002 is visible." in received
    assert "anyone benchmarked tokio?" in received
    assert f"uid={NIA}" in received
    # The only "1002" is the model's own guess and the refusal echoing it.
    _assert_no_trace(
        received.replace('"message_id":"1002"', "").replace(
            "No message with id 1002 is visible.", ""
        )
    )
    assert KAI_WORDS not in received
    assert str(KAI) not in received


async def test_proactive_watcher_input_has_placeholder_only(
    kai_blocked, proactive_run
):
    bot = proactive_run.bot
    history = await proactive._fetch_history(bot, CHANNEL, exclude_ids=set())
    new = [
        proactive.channel_message_from_hikari(
            _message(2003, NIA_USER, "anyone around?", minutes=6)
        )
    ]
    captured = {}

    class _Watcher:
        async def decide(self, **kwargs):
            captured.update(kwargs)
            return WatcherDecision(wake=False, reason="quiet"), {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_tokens": 0,
            }

    producer = WatcherProducer(
        watcher=_Watcher(),
        instruction_store=InstructionStore(seed="seed"),
        watcher_model_id="w",
        notification_queue=NotificationQueue(),
    )
    await producer.produce(
        ActivationContext(
            channel_name="general",
            guild_name="Smarter Dev",
            bot_user_id=str(BOT),
            activated_at=T0,
            history=history,
            new_messages=new,
            channel_id=str(CHANNEL),
        )
    )

    text = repr(captured)
    assert BLOCKED_PLACEHOLDER in captured["context_transcript"]
    assert "(reply to id=1002)" not in text
    _assert_no_trace(text)


async def test_proactive_recovery_skips_blocked_messages(
    kai_blocked, proactive_run, monkeypatch
):
    monkeypatch.setattr(proactive, "CATCHUP_MAX_AGE_SECONDS", 10**9)

    missed = await proactive._fetch_missed(proactive_run.bot, CHANNEL, "1000")

    assert [message.id for message in missed] == ["1001", "1003"]
    assert missed[1].reply_to_id is None  # it replied to kai
    assert missed[1].mention_user_ids == ()
    assert missed[1].content.startswith("@[blocked user] ")


async def test_external_guild_envelopes_never_carry_blocked_content(
    kai_blocked, proactive_run, monkeypatch
):
    monkeypatch.setenv(proactive.EXTERNAL_GUILDS_ENV_VAR, str(GUILD))
    run = proactive.ProactiveRuntime(proactive_run.bot, start_consumers=False)
    monkeypatch.setattr(proactive, "runtime", run)
    published = []
    queue = SimpleNamespace(
        set_execution_owner=AsyncMock(),
        publish=AsyncMock(side_effect=lambda envelope: published.append(envelope)),
        publish_shadow=AsyncMock(),
    )
    run.redis_notification_queue = lambda: queue

    kai_mention = _message(2001, KAI_USER, f"<@{BOT}> {KAI_WORDS}", mentions=(BOT,))
    await proactive.on_guild_message(_event(kai_mention))
    assert published == []

    nia_reply = _message(
        2004,
        NIA_USER,
        f"<@{BOT}> did <@!{KAI}> post it?",
        mentions=(BOT, KAI),
        reply_to=_channel_messages()[1],
        minutes=7,
    )
    await proactive.on_guild_message(_event(nia_reply))

    assert published  # mode change + mention
    bodies = "\n".join(envelope.model_dump_json() for envelope in published)
    assert "@[blocked user]" in bodies
    _assert_no_trace(bodies)
    assert not run.guild_states  # external: nothing runs in-process


async def test_proactive_cold_start_defers_wakes_and_ingests_nothing(
    proactive_run,
):
    get_blocked_users().reset()
    run = proactive_run.run
    nia_mention = _message(2002, NIA_USER, f"<@{BOT}> hi", mentions=(BOT,))

    await proactive.on_guild_message(_event(nia_mention))
    assert not run.channel_states
    assert not run.guild_states

    # A wake queued before the list was lost is deferred, not answered.
    guild_state = run.guild_state_for(GUILD)
    guild_state.queue.push(
        proactive.mention_notification(
            proactive.channel_message_from_hikari(nia_mention), channel_id="1"
        )
    )
    model, seen = _capturing_model()
    run.agent_runner_for = lambda state: KimiAgentRunner(
        agent=build_kimi_agent(model, system_prompt="test"), summarize=AsyncMock()
    )
    proactive.SETTINGS_RETRY_BACKOFF_SECONDS  # noqa: B018 — documented backoff
    original = proactive.SETTINGS_RETRY_BACKOFF_SECONDS
    proactive.SETTINGS_RETRY_BACKOFF_SECONDS = 0
    try:
        await proactive._consume_guild_once(guild_state)
    finally:
        proactive.SETTINGS_RETRY_BACKOFF_SECONDS = original
    assert seen == []
    assert guild_state.queue.items  # still pending for after the list loads


async def test_proactive_producer_defers_buffer_on_cold_start(proactive_run):
    get_blocked_users().reset()
    state = proactive_run.run.state_for(GUILD, CHANNEL)
    state.buffer.append(object())

    await proactive._run_producer_once(state)

    assert len(state.buffer) == 1


async def test_external_guild_publishes_nothing_for_blocked_reply_or_reaction(
    kai_blocked, proactive_run, monkeypatch
):
    monkeypatch.setenv(proactive.EXTERNAL_GUILDS_ENV_VAR, str(GUILD))
    run = proactive.ProactiveRuntime(proactive_run.bot, start_consumers=False)
    monkeypatch.setattr(proactive, "runtime", run)
    published = []
    run.redis_notification_queue = lambda: SimpleNamespace(
        set_execution_owner=AsyncMock(),
        publish=AsyncMock(side_effect=published.append),
        publish_shadow=AsyncMock(side_effect=published.append),
    )
    bot_message = _channel_messages()[3]
    kai_reply = _message(
        2005, KAI_USER, KAI_WORDS, reply_to=bot_message, minutes=8,
        attachments=[KAI_ATTACHMENT],
    )

    await proactive.on_guild_message(_event(kai_reply))
    await proactive.on_guild_reaction(
        SimpleNamespace(
            guild_id=GUILD, channel_id=CHANNEL, user_id=KAI, message_id=1004,
            member=SimpleNamespace(display_name="kai"), emoji_name="👍",
        )
    )

    assert published == []
    assert not run.channel_states  # nothing buffered for a later watcher batch


async def test_raw_mentions_of_blocked_user_are_scrubbed_everywhere(
    kai_blocked, chat_bot
):
    """Even outside the mention list: a code block in nia's message and the
    bot's own earlier message."""
    code = _message(
        1008, NIA_USER, f"```\nping(<@!{KAI}>)\n```", minutes=5, mentions=()
    )
    own = _message(1009, BOT_USER, f"thanks <@{KAI}>!", minutes=6)
    bot = _fake_bot([*chat_bot, code, own])

    agent_input = await chat_context.build_followup_input(
        bot=bot, channel_id=CHANNEL, guild_id=GUILD, queued=[code, own],
        memory=_Memory(),
    )
    prompt, history = build_agent_call(agent_input, [])
    chat_text = prompt + ModelMessagesTypeAdapter.dump_json(history).decode()
    proactive_text = "\n".join(
        proactive.channel_message_from_hikari(m).content for m in (code, own)
    )

    for text in (chat_text, proactive_text):
        assert text.count("@[blocked user]") == 2
        _assert_no_trace(text)


async def test_watcher_summary_envelope_carries_no_blocked_content(
    kai_blocked, proactive_run, monkeypatch
):
    """A watcher that quotes everything it saw still has nothing of kai's to
    quote: its input held only the placeholder."""
    monkeypatch.setenv(proactive.EXTERNAL_GUILDS_ENV_VAR, str(GUILD))
    run = proactive.ProactiveRuntime(proactive_run.bot, start_consumers=False)
    monkeypatch.setattr(proactive, "runtime", run)
    published = []
    run.redis_notification_queue = lambda: SimpleNamespace(
        set_execution_owner=AsyncMock(),
        publish=AsyncMock(side_effect=published.append),
        publish_shadow=AsyncMock(side_effect=published.append),
    )

    class _Parrot:
        async def decide(self, **kwargs):
            return (
                WatcherDecision(
                    wake=True,
                    reason="parrot",
                    summary=kwargs["context_transcript"] + kwargs["new_transcript"],
                    relevant_message_ids=list(kwargs["new_message_ids"]),
                ),
                {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
            )

    monkeypatch.setattr(run, "watcher", lambda: _Parrot())
    state = run.state_for(GUILD, CHANNEL)
    state.buffer.append(
        proactive.channel_message_from_hikari(
            _message(2006, NIA_USER, "what was that benchmark?", minutes=9)
        )
    )

    await proactive._run_producer_once(state, passive=True)

    bodies = "\n".join(envelope.body for envelope in published)
    assert "what was that benchmark?" in bodies
    assert BLOCKED_PLACEHOLDER in bodies
    _assert_no_trace(bodies)
