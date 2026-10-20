"""Whole-forum support for the proactive agent (#248).

A forum switched on as a whole has one settings row, the forum's. Each of
its posts is a thread with its own id: a post with no row of its own runs on
the forum's settings, while its buffer, review cursor, wakes and replies stay
the post's own. A row for the post itself wins. Text-channel threads never
inherit, and switching a forum on wakes nobody.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hikari
import httpx
import pytest

from smarter_dev.bot import forum_posts
from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.agent import ToolBudget
from smarter_dev.bot.proactive.environment import WakeActions
from smarter_dev.bot.proactive.notifications import mention_notification
from smarter_dev.bot.proactive.notifications import watcher_summary_notification
from smarter_dev.bot.proactive.types import ActivationResult
from smarter_dev.bot.proactive.types import ProposedResponse
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.proactive_settings_service import EnabledProactiveChannel
from smarter_dev.bot.services.proactive_settings_service import ProactiveChannelSettings
from smarter_dev.bot.services.proactive_settings_service import ProactiveSettingsService
from smarter_dev.shared.proactive_channel_wake import ChannelEnabledRequest

GUILD = 2
FORUM = 10
POST = 11
OTHER_POST = 12
TEXT = 20
TEXT_THREAD = 21
UNCACHED_POST = 13
TAG_PY = 901
TAG_HELP = 902


@pytest.fixture(autouse=True)
def glm_schedule(monkeypatch):
    """The GLM schedule leaves passive buffers alone, so a test runs the
    producer itself."""
    monkeypatch.setenv(proactive.WATCHER_MODEL_ENV_VAR, "z-ai/glm-5.3-flash")


def _addendum(text: str) -> str:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return json.dumps([{"id": "w1", "text": text, "expires_at": expires}])


class _Settings:
    """Rows by channel id; a channel without one gets the 404 default."""

    def __init__(self, rows: dict[int, bool] | None = None, addenda=None):
        self.rows = {str(cid): on for cid, on in (rows or {}).items()}
        # Rows marked as a whole forum's.
        self.forums: set[str] = set()
        self.addenda = {str(cid): text for cid, text in (addenda or {}).items()}
        self.lookups: list[str] = []
        self.set_calls: list[tuple[str, str, bool]] = []
        self.saved_addenda: list[tuple[str, str]] = []

    async def get_settings(self, guild_id, channel_id, *, fresh=False):
        self.lookups.append(str(channel_id))
        if str(channel_id) not in self.rows:
            return ProactiveChannelSettings.default(str(guild_id), str(channel_id))
        return ProactiveChannelSettings(
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            enabled=self.rows[str(channel_id)],
            watch_addendum=self.addenda.get(str(channel_id), ""),
            is_forum=str(channel_id) in self.forums,
        )

    async def list_enabled_channels(self, guild_id):
        return [
            EnabledProactiveChannel(
                channel_id=cid,
                watch_addendum=self.addenda.get(cid, ""),
                is_forum=cid in self.forums,
            )
            for cid, on in self.rows.items()
            if on
        ]

    async def set_enabled(self, guild_id, channel_id, enabled, *, is_forum=None):
        self.set_calls.append((str(guild_id), str(channel_id), enabled))
        self.rows[str(channel_id)] = enabled
        if is_forum:
            self.forums.add(str(channel_id))

    async def set_watch_addendum(self, guild_id, channel_id, addendum):
        self.saved_addenda.append((str(channel_id), addendum))

    async def record_wake_usage(self, *args, **kwargs):
        pass


class _History:
    def limit(self, _n):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _bot(service, *, cache_posts: bool = True):
    tags = [
        SimpleNamespace(id=TAG_PY, name="python"),
        SimpleNamespace(id=TAG_HELP, name="help"),
    ]
    channels = {
        FORUM: SimpleNamespace(
            id=FORUM,
            name="help-forum",
            type=hikari.ChannelType.GUILD_FORUM,
            available_tags=tags,
        ),
        TEXT: SimpleNamespace(
            id=TEXT, name="general", type=hikari.ChannelType.GUILD_TEXT
        ),
    }
    threads = {
        POST: SimpleNamespace(
            id=POST,
            name="How do I sort a dict?",
            parent_id=FORUM,
            type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
            applied_tag_ids=[TAG_PY, TAG_HELP],
        ),
        OTHER_POST: SimpleNamespace(
            id=OTHER_POST,
            name="Async help",
            parent_id=FORUM,
            type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
            applied_tag_ids=[],
        ),
        TEXT_THREAD: SimpleNamespace(
            id=TEXT_THREAD,
            name="side chat",
            parent_id=TEXT,
            type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
            applied_tag_ids=[],
        ),
    }
    # An archived post the cache dropped: only REST knows it.
    uncached = {
        UNCACHED_POST: SimpleNamespace(
            id=UNCACHED_POST,
            name="Old question",
            parent_id=FORUM,
            type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
            applied_tag_ids=[TAG_PY],
        )
    }
    if not cache_posts:
        uncached.update(threads)
        threads = {}

    async def fetch_channel(channel_id):
        channel_id = int(channel_id)
        found = (
            channels.get(channel_id)
            or threads.get(channel_id)
            or uncached.get(channel_id)
        )
        if found is None:
            raise AssertionError(f"unexpected fetch of {channel_id}")
        return found

    return SimpleNamespace(
        d={"proactive_settings_service": service},
        get_me=lambda: SimpleNamespace(id=999, username="smarter-bot"),
        cache=SimpleNamespace(
            get_guild_channel=lambda cid: channels.get(int(cid)),
            get_thread=lambda cid: threads.get(int(cid)),
            get_guild=lambda gid: SimpleNamespace(name="Smarter Dev"),
            get_guilds_view=lambda: {GUILD: SimpleNamespace(id=GUILD)},
            get_message=lambda _mid: None,
        ),
        rest=SimpleNamespace(
            fetch_channel=AsyncMock(side_effect=fetch_channel),
            fetch_messages=lambda *_a, **_k: _History(),
            create_message=AsyncMock(),
            add_reaction=AsyncMock(),
        ),
    )


def _message(message_id: int, *, content: str = "hello", mentions=()):
    return SimpleNamespace(
        id=message_id,
        created_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
        author=SimpleNamespace(
            id=901, username="alice", global_name="Alice", is_bot=False
        ),
        member=None,
        content=content,
        type=0,
        referenced_message=None,
        user_mentions_ids=mentions,
        mentions_everyone=False,
        attachments=[],
        stickers=[],
    )


def _event(channel_id: int, message):
    return SimpleNamespace(
        author=message.author, guild_id=GUILD, channel_id=channel_id, message=message
    )


class _Consumer:
    """Stands in for AgentConsumer: builds the agent's deps the way it does,
    drains the queue, and answers in ``respond_in``."""

    def __init__(self, kwargs, respond_in, midrun):
        self.kwargs = kwargs
        self.respond_in = respond_in
        self.midrun = midrun
        self.deps = None

    async def consume(self, context):
        self.deps = self.kwargs["deps_factory"](
            enabled_channels=dict(self.kwargs["enabled_channels"]),
            channel_envs=self.kwargs["channel_envs"],
            actions=WakeActions(),
            instruction_stores=dict(self.kwargs["instruction_stores"]),
            skim_transcript=None,
            budget=ToolBudget(),
        )
        self.at_start = dict(self.deps.enabled_channels)
        self.drained = self.kwargs["notification_queue"].drain()[0]
        # Arrivals go to the guild's live queue, as enqueue_notification's do.
        live_queue = proactive._runtime().guild_state_for(GUILD).queue
        for notification in self.midrun:
            live_queue.push(notification)
        if self.midrun:
            await self.deps.drain_notifications()
        stores = self.kwargs["instruction_stores"]
        if str(POST) in stores:
            stores[str(POST)].set_instruction(
                "watch python follow-ups", ttl_seconds=3600
            )
        return ActivationResult(
            responses=[
                ProposedResponse(reply_to_id=None, content="hi", channel_id=str(cid))
                for cid in self.respond_in
            ],
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            model_id="stub",
        )


@pytest.fixture
def forum_setup(monkeypatch):
    def build(rows, *, addenda=None, respond_in=(), midrun=(), cache_posts=True):
        service = _Settings(rows, addenda)
        bot = _bot(service, cache_posts=cache_posts)
        run = proactive.ProactiveRuntime(bot, start_consumers=False)
        monkeypatch.setattr(proactive, "runtime", run)
        monkeypatch.setattr(run, "watcher", lambda: None)
        monkeypatch.setattr(run, "skim", lambda: None)
        monkeypatch.setattr(run, "agent_runner_for", lambda state: None)
        produced = []
        consumers = []

        class Producer:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.details = {"watcher": {"wake": True}}
                self.wake_produced = True

            async def produce(self, context):
                produced.append((context, self.kwargs["instruction_store"]))
                self.kwargs["notification_queue"].push(
                    watcher_summary_notification(
                        summary="a question",
                        message_ids=[m.id for m in context.new_messages],
                        wake=True,
                        created_at=context.activated_at,
                        channel_id=context.channel_id,
                        channel_name=context.channel_name,
                    )
                )
                return {}

        def consumer_factory(**kwargs):
            consumer = _Consumer(kwargs, respond_in, midrun)
            consumers.append(consumer)
            return consumer

        monkeypatch.setattr(proactive, "WatcherProducer", Producer)
        monkeypatch.setattr(proactive, "AgentConsumer", consumer_factory)
        return SimpleNamespace(
            service=service, bot=bot, run=run, produced=produced, consumers=consumers
        )

    return build


async def _wake(setup) -> None:
    state = setup.run.guild_state_for(GUILD)
    if state.queue.items:
        await proactive._consume_guild_once(state)


# --- inheritance ---------------------------------------------------------------


async def test_a_post_with_no_row_runs_on_its_forums_settings(forum_setup):
    setup = forum_setup({FORUM: True}, addenda={FORUM: _addendum("python questions")})

    await proactive.on_guild_message(_event(POST, _message(500)))
    state = setup.run.channel_states[POST]
    await proactive._run_producer(state)

    assert [m.id for m in state.buffer] == []
    context, store = setup.produced[0]
    assert context.channel_id == str(POST)
    assert context.channel_name == "How do I sort a dict?"
    # The forum's watch instructions, not an empty store of the post's.
    assert [entry.text for entry in store.entries] == ["python questions"]


async def test_a_post_switched_off_on_its_own_stays_off(forum_setup):
    setup = forum_setup({FORUM: True, POST: False})

    await proactive.on_guild_message(_event(POST, _message(500)))

    assert POST not in setup.run.channel_states


async def test_a_post_switched_on_on_its_own_needs_no_forum(forum_setup):
    setup = forum_setup({POST: True})

    await proactive.on_guild_message(_event(POST, _message(500)))

    assert setup.run.channel_states[POST].buffer
    # Its own row answered; nothing asked about its forum.
    assert setup.service.lookups == [str(POST)]


async def test_a_post_of_a_forum_that_is_off_stays_off(forum_setup):
    setup = forum_setup({FORUM: False})

    await proactive.on_guild_message(_event(POST, _message(500)))

    assert POST not in setup.run.channel_states


async def test_a_text_channel_thread_does_not_inherit(forum_setup):
    setup = forum_setup({TEXT: True})

    await proactive.on_guild_message(_event(TEXT_THREAD, _message(500)))

    assert TEXT_THREAD not in setup.run.channel_states
    assert str(TEXT) not in setup.service.lookups


async def test_an_archived_post_out_of_the_cache_inherits_through_rest(forum_setup):
    """Writing in an archived post unarchives it; the post may no longer be
    cached, so its forum comes from REST, once."""
    setup = forum_setup({FORUM: True})

    await proactive.on_guild_message(_event(UNCACHED_POST, _message(500)))
    await proactive.on_guild_message(_event(UNCACHED_POST, _message(501)))

    assert len(setup.run.channel_states[UNCACHED_POST].buffer) == 2
    fetched = [call.args[0] for call in setup.bot.rest.fetch_channel.await_args_list]
    assert fetched.count(UNCACHED_POST) == 1


async def test_reactions_in_a_post_inherit_too(forum_setup):
    setup = forum_setup({FORUM: True})
    bot_message = SimpleNamespace(
        author=SimpleNamespace(id=999), content="my answer", referenced_message=None
    )
    setup.bot.rest.fetch_message = AsyncMock(return_value=bot_message)
    event = SimpleNamespace(
        guild_id=GUILD,
        channel_id=POST,
        message_id=700,
        user_id=42,
        member=SimpleNamespace(display_name="bob"),
        emoji_name="👍",
    )

    await proactive.on_guild_reaction(event)

    (queued,) = setup.run.guild_state_for(GUILD).queue.items
    assert (queued.kind, queued.channel_id) == ("reaction", str(POST))


# --- a new post's title and tags --------------------------------------------------


async def test_a_posts_opening_message_carries_its_title_and_tags(forum_setup):
    setup = forum_setup({FORUM: True})

    # A post's opening message has the post's own id.
    await proactive.on_guild_message(_event(POST, _message(POST, content="see title")))

    (opening,) = setup.run.channel_states[POST].buffer
    assert opening.content == (
        '[Opened a new forum post titled "How do I sort a dict?" '
        "(tags: python, help)]\nsee title"
    )


async def test_a_later_message_in_the_post_is_left_as_written(forum_setup):
    setup = forum_setup({FORUM: True})

    await proactive.on_guild_message(_event(POST, _message(501, content="thanks")))

    assert setup.run.channel_states[POST].buffer[0].content == "thanks"


async def test_an_opening_mention_reaches_the_agent_with_title_and_tags(forum_setup):
    setup = forum_setup({FORUM: True})

    await proactive.on_guild_message(
        _event(POST, _message(POST, content="<@999> help?", mentions=(999,)))
    )

    mention = next(
        n for n in setup.run.guild_state_for(GUILD).queue.items if n.kind == "mention"
    )
    assert 'titled "How do I sort a dict?" (tags: python, help)' in mention.body
    assert mention.channel_name == "How do I sort a dict?"


async def test_an_uncached_posts_tags_are_read_from_its_forum(forum_setup):
    setup = forum_setup({FORUM: True})

    await proactive.on_guild_message(
        _event(UNCACHED_POST, _message(UNCACHED_POST, content="q"))
    )

    (opening,) = setup.run.channel_states[UNCACHED_POST].buffer
    assert opening.content.startswith(
        '[Opened a new forum post titled "Old question" (tags: python)]'
    )


# --- each post its own channel ---------------------------------------------------


async def test_two_posts_keep_separate_buffers_cursors_and_replies(forum_setup):
    setup = forum_setup({FORUM: True}, respond_in=(POST, OTHER_POST))

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive.on_guild_message(_event(OTHER_POST, _message(501)))
    first = setup.run.channel_states[POST]
    second = setup.run.channel_states[OTHER_POST]
    assert first is not second
    await proactive._run_producer(first)
    await proactive._run_producer(second)
    await _wake(setup)

    assert (first.last_reviewed_message_id, second.last_reviewed_message_id) == (
        "500",
        "501",
    )
    (consumer,) = setup.consumers
    labels = consumer.at_start
    assert labels == {
        str(
            POST
        ): "How do I sort a dict? (post in forum #help-forum; tags: python, help)",
        str(OTHER_POST): "Async help (post in forum #help-forum)",
    }
    sent_to = [call.args[0] for call in setup.bot.rest.create_message.await_args_list]
    assert sent_to == [POST, OTHER_POST]


async def test_the_forum_itself_is_never_a_channel_the_agent_acts_in(forum_setup):
    setup = forum_setup({FORUM: True, TEXT: True}, respond_in=(FORUM, POST))

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    await _wake(setup)

    (consumer,) = setup.consumers
    assert set(consumer.at_start) == {str(TEXT), str(POST)}
    sent_to = [call.args[0] for call in setup.bot.rest.create_message.await_args_list]
    assert sent_to == [POST]


async def test_a_post_that_arrives_mid_wake_can_be_answered_in_it(forum_setup):
    later = mention_notification(
        proactive.channel_message_from_hikari(_message(600)),
        channel_id=str(OTHER_POST),
        channel_name="Async help",
    )
    setup = forum_setup({FORUM: True}, respond_in=(OTHER_POST,), midrun=(later,))
    await forum_posts.forum_parents(setup.bot).forum_for(str(OTHER_POST))

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    await _wake(setup)

    (consumer,) = setup.consumers
    assert str(OTHER_POST) not in consumer.at_start
    assert str(OTHER_POST) in consumer.deps.enabled_channels
    sent_to = [call.args[0] for call in setup.bot.rest.create_message.await_args_list]
    assert sent_to == [OTHER_POST]


async def test_watch_instructions_set_in_a_post_are_saved_on_the_forum(forum_setup):
    setup = forum_setup({FORUM: True})

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    await _wake(setup)

    assert [channel for channel, _ in setup.service.saved_addenda] == [str(FORUM)]
    assert "watch python follow-ups" in setup.service.saved_addenda[0][1]


async def test_only_a_forum_on_and_nothing_from_its_posts_runs_no_agent(forum_setup):
    setup = forum_setup({FORUM: True})
    setup.run.guild_state_for(GUILD).queue.push(
        mention_notification(
            proactive.channel_message_from_hikari(_message(500)),
            channel_id=str(TEXT_THREAD),
            channel_name="side chat",
        )
    )

    await _wake(setup)

    assert setup.consumers == []
    assert setup.run.guild_state_for(GUILD).queue.items == []


async def test_the_agent_switching_a_posts_mode_touches_only_that_post(
    forum_setup, monkeypatch
):
    setup = forum_setup({FORUM: True})
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive.on_guild_message(_event(OTHER_POST, _message(501)))
    await proactive._run_producer(setup.run.channel_states[POST])

    async def consume(self, context):
        self.deps = self.kwargs["deps_factory"](
            enabled_channels={},
            channel_envs=None,
            actions=WakeActions(),
            instruction_stores={},
            skim_transcript=None,
            budget=ToolBudget(),
        )
        self.deps.request_mode(str(POST), "active", 5)
        return ActivationResult(
            responses=[],
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            model_id="stub",
        )

    monkeypatch.setattr(_Consumer, "consume", consume)
    await _wake(setup)

    assert setup.run.channel_states[POST].active_until > proactive.time.monotonic()
    assert setup.run.channel_states[OTHER_POST].active_until == 0.0


# --- switching a forum on wakes nobody -------------------------------------------


def _ctx(channel_id: int, *, whole_forum: bool):
    return SimpleNamespace(
        guild_id=GUILD,
        channel_id=channel_id,
        options=SimpleNamespace(whole_forum=whole_forum),
        respond=AsyncMock(),
    )


@pytest.fixture
def moderator(monkeypatch):
    monkeypatch.setattr(
        proactive, "deny_without_moderator_permissions", AsyncMock(return_value=False)
    )


async def test_whole_forum_on_switches_the_forum_without_a_wake(forum_setup, moderator):
    setup = forum_setup({})

    await proactive.proactive_on.callback(_ctx(POST, whole_forum=True))

    assert setup.service.set_calls == [(str(GUILD), str(FORUM), True)]
    # Marked, so a worker that never asked for forum rows is not shown it.
    assert setup.service.forums == {str(FORUM)}
    assert setup.run.guild_state_for(GUILD).queue.items == []


async def test_whole_forum_off_switches_the_forum_off(forum_setup, moderator):
    setup = forum_setup({FORUM: True})

    await proactive.proactive_off.callback(_ctx(POST, whole_forum=True))

    assert setup.service.set_calls == [(str(GUILD), str(FORUM), False)]


async def test_off_in_a_post_turns_off_just_that_post(forum_setup, moderator):
    setup = forum_setup({FORUM: True})

    await proactive.proactive_off.callback(_ctx(POST, whole_forum=False))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive.on_guild_message(_event(OTHER_POST, _message(501)))

    assert setup.service.set_calls == [(str(GUILD), str(POST), False)]
    assert POST not in setup.run.channel_states
    assert OTHER_POST in setup.run.channel_states


async def test_whole_forum_outside_a_forum_post_is_refused(forum_setup, moderator):
    setup = forum_setup({})
    ctx = _ctx(TEXT_THREAD, whole_forum=True)

    await proactive.proactive_on.callback(ctx)

    assert setup.service.set_calls == []
    assert "forum post" in ctx.respond.await_args.args[0]


async def test_the_web_switching_a_forum_on_wakes_nobody(forum_setup):
    setup = forum_setup({FORUM: True})
    setup.service.invalidate = lambda *_: None
    for post in (POST, OTHER_POST):
        await forum_posts.forum_parents(setup.bot).forum_for(str(post))

    await proactive._apply_channel_enabled_request(
        setup.run,
        ChannelEnabledRequest(
            guild_id=str(GUILD), channel_id=str(FORUM), created_at=datetime.now(UTC)
        ),
    )

    assert setup.run.guild_state_for(GUILD).queue.items == []


async def test_the_web_switching_a_text_channel_on_still_wakes(forum_setup):
    setup = forum_setup({TEXT: True})
    setup.service.invalidate = lambda *_: None

    await proactive._apply_channel_enabled_request(
        setup.run,
        ChannelEnabledRequest(
            guild_id=str(GUILD), channel_id=str(TEXT), created_at=datetime.now(UTC)
        ),
    )

    (queued,) = setup.run.guild_state_for(GUILD).queue.items
    assert (queued.kind, queued.channel_id) == ("channel_enabled", str(TEXT))


async def test_status_in_a_post_says_it_runs_on_the_forum(forum_setup, moderator):
    forum_setup({FORUM: True})
    ctx = _ctx(POST, whole_forum=False)

    await proactive.proactive_status.callback(ctx)

    body = ctx.respond.await_args.args[0]
    assert "**on**" in body and f"<#{FORUM}>" in body


async def test_recovery_skips_a_forum_row(forum_setup):
    setup = forum_setup({FORUM: True})
    store = SimpleNamespace(cursor_channel_ids=AsyncMock(return_value=[]))
    setup.run.history_store = lambda: store

    await proactive._recover_channels(setup.run)

    assert setup.run.channel_states == {}


# --- forum parents ----------------------------------------------------------------


async def test_a_failed_parent_lookup_is_not_remembered():
    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda _cid: None, get_thread=lambda _cid: None
        ),
        rest=SimpleNamespace(
            fetch_channel=AsyncMock(
                side_effect=[RuntimeError("discord down"), _forum_post()]
            )
        ),
    )
    parents = forum_posts.forum_parents(bot)

    assert await parents.forum_for("11") is None
    bot.rest.fetch_channel.side_effect = None
    bot.rest.fetch_channel.return_value = None

    def answer(channel_id):
        if int(channel_id) == 11:
            return _forum_post()
        return SimpleNamespace(type=hikari.ChannelType.GUILD_FORUM)

    bot.rest.fetch_channel.side_effect = answer
    assert await parents.forum_for("11") == "10"


def _forum_post():
    return SimpleNamespace(type=hikari.ChannelType.GUILD_PUBLIC_THREAD, parent_id=10)


async def test_a_media_channels_posts_inherit_too():
    media_post = SimpleNamespace(
        type=hikari.ChannelType.GUILD_PUBLIC_THREAD, parent_id=30
    )
    media = SimpleNamespace(type=16)  # GUILD_MEDIA, unnamed in hikari 2.1
    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda cid: media if int(cid) == 30 else None,
            get_thread=lambda cid: media_post if int(cid) == 31 else None,
        ),
    )

    assert await forum_posts.forum_parents(bot).forum_for("31") == "30"


# --- the classic chat bot and /configure ------------------------------------------


async def test_the_classic_chat_bot_stays_out_of_an_enabled_forums_posts(forum_setup):
    from smarter_dev.bot.plugins import mention

    setup = forum_setup({FORUM: True, OTHER_POST: False})

    def event(channel_id):
        return SimpleNamespace(guild_id=GUILD, channel_id=channel_id)

    assert await mention._proactive_channel(setup.bot, event(POST)) is True
    assert await mention._proactive_channel(setup.bot, event(OTHER_POST)) is False
    assert await mention._proactive_channel(setup.bot, event(TEXT_THREAD)) is False


async def test_configure_whole_forum_links_the_forum(forum_setup, monkeypatch):
    from smarter_dev.bot.plugins import configure
    from smarter_dev.shared import config_links

    setup = forum_setup({})
    monkeypatch.setattr(
        configure, "deny_without_moderator_permissions", AsyncMock(return_value=False)
    )
    ctx = _ctx(POST, whole_forum=True)
    ctx.bot = setup.bot
    ctx.author = SimpleNamespace(id=5)

    await configure.configure_bot.callback(ctx)

    body = ctx.respond.await_args.args[0]
    assert "this whole forum" in body
    token = body.split("/admin/bot/configure/")[1].split(")")[0]
    payload = config_links.verify_config_link(token)
    assert payload.authorizes(guild_id=str(GUILD), channel_id=str(FORUM))
    assert payload.is_forum is True


async def test_configure_whole_forum_outside_a_post_is_refused(
    forum_setup, monkeypatch
):
    from smarter_dev.bot.plugins import configure

    setup = forum_setup({})
    monkeypatch.setattr(
        configure, "deny_without_moderator_permissions", AsyncMock(return_value=False)
    )
    ctx = _ctx(TEXT, whole_forum=True)
    ctx.bot = setup.bot
    ctx.author = SimpleNamespace(id=5)

    await configure.configure_bot.callback(ctx)

    assert "/admin/bot/configure/" not in ctx.respond.await_args.args[0]


async def test_a_failed_forum_check_of_the_parent_is_not_remembered():
    """A post whose forum Discord could not describe must not be remembered
    as no forum post at all."""
    calls = {"forum": 0}

    async def fetch_channel(channel_id):
        if int(channel_id) == 11:
            return _forum_post()
        calls["forum"] += 1
        if calls["forum"] == 1:
            raise RuntimeError("discord down")
        return SimpleNamespace(type=hikari.ChannelType.GUILD_FORUM)

    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda _cid: None, get_thread=lambda _cid: None
        ),
        rest=SimpleNamespace(fetch_channel=AsyncMock(side_effect=fetch_channel)),
    )
    parents = forum_posts.forum_parents(bot)

    assert await parents.forum_for("11") is None
    assert await parents.forum_for("11") == "10"
    assert await parents.forum_for("11") == "10"
    assert calls["forum"] == 2  # remembered once Discord answered


async def test_a_forbidden_channel_is_asked_again():
    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda _cid: None, get_thread=lambda _cid: None
        ),
        rest=SimpleNamespace(
            fetch_channel=AsyncMock(
                side_effect=hikari.ForbiddenError(
                    url="u", headers={}, raw_body=b"", message="no", code=50001
                )
            )
        ),
    )
    parents = forum_posts.forum_parents(bot)

    assert await parents.forum_for("11") is None
    assert await parents.forum_for("11") is None
    assert bot.rest.fetch_channel.await_count == 2


async def test_an_opted_out_authors_post_title_never_reaches_the_agent(
    forum_setup, blocked_users_list_loaded
):
    setup = forum_setup({FORUM: True}, respond_in=(POST,))
    post = setup.bot.cache.get_thread(POST)
    post.owner_id = 4242
    blocked_users_list_loaded.load(1, ["4242"])

    await proactive.on_guild_message(
        _event(POST, _message(501, content="<@999> any ideas?", mentions=(999,)))
    )
    await proactive._run_producer(setup.run.channel_states[POST])
    await _wake(setup)

    queued_names = {setup.consumers[0].at_start[str(POST)]}
    assert "How do I sort a dict?" not in str(queued_names)
    assert forum_posts.HIDDEN_POST_TITLE in setup.consumers[0].at_start[str(POST)]
    assert proactive._channel_name_for_id(setup.bot, str(POST)) == (
        forum_posts.HIDDEN_POST_TITLE
    )


# --- a post's own row, rechecked when the wake admits it ---------------------------


def _later_mention(channel_id: int, message_id: int = 600):
    return mention_notification(
        proactive.channel_message_from_hikari(_message(message_id)),
        channel_id=str(channel_id),
        channel_name="Async help",
    )


async def test_a_post_switched_off_after_it_queued_gets_no_reply(forum_setup):
    setup = forum_setup({FORUM: True}, respond_in=(POST,))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])

    await setup.service.set_enabled(str(GUILD), str(POST), False)
    await _wake(setup)

    assert setup.consumers == []
    setup.bot.rest.create_message.assert_not_awaited()


async def test_a_post_switched_off_is_not_admitted_mid_wake(forum_setup):
    setup = forum_setup(
        {FORUM: True}, respond_in=(OTHER_POST,), midrun=(_later_mention(OTHER_POST),)
    )
    await forum_posts.forum_parents(setup.bot).forum_for(str(OTHER_POST))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])

    await setup.service.set_enabled(str(GUILD), str(OTHER_POST), False)
    await _wake(setup)

    (consumer,) = setup.consumers
    assert str(OTHER_POST) not in consumer.deps.enabled_channels
    setup.bot.rest.create_message.assert_not_awaited()


async def test_a_post_whose_own_settings_cannot_be_read_is_not_admitted(forum_setup):
    setup = forum_setup({FORUM: True}, respond_in=(POST,))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    listed = setup.service.get_settings

    async def failing(guild_id, channel_id, **kwargs):
        if str(channel_id) == str(POST):
            raise RuntimeError("api down")
        return await listed(guild_id, channel_id, **kwargs)

    setup.service.get_settings = failing
    await _wake(setup)

    setup.bot.rest.create_message.assert_not_awaited()


async def test_a_post_admitted_mid_wake_has_its_forums_watch_instructions(
    forum_setup,
):
    setup = forum_setup(
        {FORUM: True},
        addenda={FORUM: _addendum("python questions")},
        midrun=(_later_mention(OTHER_POST),),
    )
    await forum_posts.forum_parents(setup.bot).forum_for(str(OTHER_POST))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])

    await _wake(setup)

    deps = setup.consumers[0].deps
    # What the watch-instruction tools read and write for that post.
    store = deps.channel_instruction_store(str(OTHER_POST))
    assert store is deps.channel_instruction_store(str(POST))
    assert "python questions" in [entry.text for entry in store.entries]
    store.set_instruction("watch async follow-ups", ttl_seconds=3600)
    # Saved on the forum, the only row.
    assert [channel for channel, _ in setup.service.saved_addenda] == [str(FORUM)]


def _real_settings_service(setup) -> ProactiveSettingsService:
    """The bot's real settings service, with its cache, over an API that
    reads the fake's rows as they are at each call."""
    rows = setup.service

    async def get(path, **_kwargs):
        if "/channels/" not in path:
            return httpx.Response(
                200,
                json=[
                    {"channel_id": cid, "watch_addendum": "", "is_forum": cid in rows.forums}
                    for cid, on in rows.rows.items()
                    if on
                ],
            )
        cid = path.split("/channels/")[1].split("/")[0]
        if cid not in rows.rows:
            raise APIError("no row", status_code=404)
        return httpx.Response(
            200,
            json={
                "guild_id": str(GUILD),
                "channel_id": cid,
                "enabled": rows.rows[cid],
                "watch_addendum": "",
                "is_forum": cid in rows.forums,
            },
        )

    service = ProactiveSettingsService(SimpleNamespace(get=AsyncMock(side_effect=get)))
    service.record_wake_usage = AsyncMock()
    service.set_watch_addendum = AsyncMock()
    setup.bot.d["proactive_settings_service"] = service
    return service


async def test_a_post_switched_off_on_the_web_after_it_queued_gets_no_reply(
    forum_setup,
):
    setup = forum_setup({FORUM: True}, respond_in=(POST,))
    setup.service.forums.add(str(FORUM))
    _real_settings_service(setup)
    # The gate looked the post up and cached that it has no row.
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])

    # The web form, another process, writes the post's own row.
    setup.service.rows[str(POST)] = False
    await _wake(setup)

    setup.bot.rest.create_message.assert_not_awaited()


async def test_a_post_switched_off_on_the_web_is_not_admitted_mid_wake(
    forum_setup,
):
    setup = forum_setup(
        {FORUM: True}, respond_in=(OTHER_POST,), midrun=(_later_mention(OTHER_POST),)
    )
    setup.service.forums.add(str(FORUM))
    service = _real_settings_service(setup)
    await forum_posts.forum_parents(setup.bot).forum_for(str(OTHER_POST))
    assert not (await service.get_settings(str(GUILD), str(OTHER_POST))).exists
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])

    setup.service.rows[str(OTHER_POST)] = False
    await _wake(setup)

    (consumer,) = setup.consumers
    assert str(OTHER_POST) not in consumer.deps.enabled_channels
    setup.bot.rest.create_message.assert_not_awaited()


async def test_a_post_arriving_during_admission_waits_for_a_drain_that_admits_it(
    forum_setup,
):
    setup = forum_setup({FORUM: True}, respond_in=(OTHER_POST,))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    listed = setup.service.get_settings
    arrived = False

    async def get_settings(guild_id, channel_id, **kwargs):
        nonlocal arrived
        if str(channel_id) == str(POST) and not arrived:
            arrived = True
            # A second post's message while admission awaits the API.
            await proactive.on_guild_message(_event(OTHER_POST, _message(501)))
            await proactive._run_producer(setup.run.channel_states[OTHER_POST])
        return await listed(guild_id, channel_id, **kwargs)

    setup.service.get_settings = get_settings
    await _wake(setup)

    # This wake had only the batch it admitted; the reply to the other
    # post, not admitted, was not sent, and its message is still queued.
    first = setup.consumers[0]
    assert {item.channel_id for item in first.drained} == {str(POST)}
    assert str(OTHER_POST) not in first.at_start
    setup.bot.rest.create_message.assert_not_awaited()
    queue = setup.run.guild_state_for(GUILD).queue
    assert {item.channel_id for item in queue.items} == {str(OTHER_POST)}

    # The next wake admits it and answers it.
    await _wake(setup)

    assert str(OTHER_POST) in setup.consumers[1].at_start
    assert setup.bot.rest.create_message.await_args.args[0] == OTHER_POST
    assert queue.items == []


# --- a channel Discord cannot type is not acted on ---------------------------------


def _discord_down_for(setup, *channel_ids):
    """Drop the channels from the cache and make REST fail for them."""
    down = {int(cid) for cid in channel_ids}
    cached = setup.bot.cache.get_guild_channel
    setup.bot.cache.get_guild_channel = lambda cid: (
        None if int(cid) in down else cached(cid)
    )
    fetch = setup.bot.rest.fetch_channel.side_effect

    async def fetch_channel(channel_id):
        if int(channel_id) in down:
            raise RuntimeError("discord down")
        return await fetch(channel_id)

    setup.bot.rest.fetch_channel.side_effect = fetch_channel


async def test_the_web_switching_on_a_channel_discord_cannot_type_waits(forum_setup):
    setup = forum_setup({FORUM: True})
    setup.service.invalidate = lambda *_: None
    _discord_down_for(setup, FORUM)
    request = ChannelEnabledRequest(
        guild_id=str(GUILD), channel_id=str(FORUM), created_at=datetime.now(UTC)
    )

    with pytest.raises(proactive.ChannelTypeUnknownError):
        await proactive._apply_channel_enabled_request(setup.run, request)

    # Left pending (not acknowledged) and no wake sent.
    assert setup.run.guild_state_for(GUILD).queue.items == []


async def test_a_web_wake_for_a_marked_forum_needs_no_discord(forum_setup):
    setup = forum_setup({FORUM: True})
    setup.service.forums.add(str(FORUM))
    setup.service.invalidate = lambda *_: None
    _discord_down_for(setup, FORUM)

    await proactive._apply_channel_enabled_request(
        setup.run,
        ChannelEnabledRequest(
            guild_id=str(GUILD), channel_id=str(FORUM), created_at=datetime.now(UTC)
        ),
    )

    assert setup.run.guild_state_for(GUILD).queue.items == []


async def test_a_wake_leaves_out_a_row_discord_cannot_type(forum_setup):
    setup = forum_setup({FORUM: True, TEXT: True}, respond_in=(FORUM, TEXT))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    _discord_down_for(setup, FORUM)

    await _wake(setup)

    (consumer,) = setup.consumers
    assert str(FORUM) not in consumer.at_start
    sent_to = [call.args[0] for call in setup.bot.rest.create_message.await_args_list]
    assert FORUM not in sent_to


async def test_a_marked_forum_row_is_a_forum_without_asking_discord(forum_setup):
    setup = forum_setup({FORUM: True}, respond_in=(FORUM, POST))
    setup.service.forums.add(str(FORUM))
    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    _discord_down_for(setup, FORUM)

    await _wake(setup)

    (consumer,) = setup.consumers
    assert set(consumer.at_start) == {str(POST)}


async def test_recovery_skips_a_row_discord_cannot_type(forum_setup):
    setup = forum_setup({FORUM: True})
    store = SimpleNamespace(cursor_channel_ids=AsyncMock(return_value=[]))
    setup.run.history_store = lambda: store
    _discord_down_for(setup, FORUM)

    await proactive._recover_channels(setup.run)

    assert setup.run.channel_states == {}


# --- an external guild's posts go only to a worker that runs them ------------------


class _Redis:
    def __init__(self, keys=()):
        self.keys = set(keys)
        self.checks = 0

    async def exists(self, key):
        self.checks += 1
        return int(key in self.keys)


def _external(setup, monkeypatch, redis):
    monkeypatch.setattr(setup.run, "external_guild_ids", {str(GUILD)})
    setup.bot.d["chat_memory_redis"] = redis


async def test_an_external_guilds_post_is_held_without_a_forum_worker(
    forum_setup, monkeypatch
):
    setup = forum_setup({FORUM: True, TEXT: True})
    _external(setup, monkeypatch, _Redis())

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive.on_guild_message(_event(TEXT, _message(501)))

    # The post never reaches the producer (or the worker); a plain channel does.
    assert POST not in setup.run.channel_states
    assert TEXT in setup.run.channel_states


async def test_an_external_guilds_post_runs_once_a_worker_says_it_can(
    forum_setup, monkeypatch
):
    setup = forum_setup({FORUM: True})
    redis = _Redis({proactive.FORUM_POSTS_CAPABILITY_KEY})
    _external(setup, monkeypatch, redis)

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive.on_guild_message(_event(POST, _message(501)))

    assert POST in setup.run.channel_states
    assert redis.checks == 1  # cached between messages
    assert proactive.FORUM_POSTS_CAPABILITY_KEY == (
        "proactive:v1:capability:forum-posts"
    )


async def test_an_external_guilds_post_goes_where_only_a_forum_worker_reads(
    forum_setup, monkeypatch
):
    import fakeredis.aioredis

    from smarter_dev.bot.proactive import redis_queue

    setup = forum_setup({FORUM: True, TEXT: True})
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set(proactive.FORUM_POSTS_CAPABILITY_KEY, b"1")
    _external(setup, monkeypatch, redis)

    await proactive.on_guild_message(_event(POST, _message(500)))
    await proactive._run_producer(setup.run.channel_states[POST])
    await proactive.on_guild_message(_event(TEXT, _message(501)))
    await proactive._run_producer(setup.run.channel_states[TEXT])

    async def channels(stream):
        return [
            json.loads(fields[b"payload"])["channel_id"]
            for _id, fields in await redis.xrange(stream)
        ]

    assert await channels(redis_queue.forum_wake_stream_key(str(GUILD))) == [str(POST)]
    assert await channels(redis_queue.wake_stream_key(str(GUILD))) == [str(TEXT)]
    assert await redis.xlen(redis_queue.FORUM_READY_STREAM_KEY) == 1
    assert await redis.xlen(redis_queue.READY_STREAM_KEY) == 1


async def test_an_unreadable_capability_counts_as_no_forum_worker(
    forum_setup, monkeypatch
):
    setup = forum_setup({FORUM: True})

    class _Down:
        async def exists(self, _key):
            raise RuntimeError("redis down")

    _external(setup, monkeypatch, _Down())

    await proactive.on_guild_message(_event(POST, _message(500)))

    assert POST not in setup.run.channel_states


@pytest.mark.parametrize("forum_on", [True, False])
async def test_a_post_with_its_own_row_is_not_held(forum_setup, monkeypatch, forum_on):
    import fakeredis.aioredis

    from smarter_dev.bot.proactive import redis_queue

    setup = forum_setup({FORUM: forum_on, POST: True})
    # No worker says it runs forum posts: an older worker.
    redis = fakeredis.aioredis.FakeRedis()
    _external(setup, monkeypatch, redis)

    await proactive.on_guild_message(_event(POST, _message(500)))

    # A row of its own is a channel any worker runs.
    assert POST in setup.run.channel_states
    await proactive._run_producer(setup.run.channel_states[POST])

    # Published where it always was, so an older worker takes it: its group
    # reads the shared ready stream, then the guild's shared wake stream.
    assert await redis.exists(redis_queue.forum_wake_stream_key(str(GUILD))) == 0
    for stream in (
        redis_queue.READY_STREAM_KEY,
        redis_queue.wake_stream_key(str(GUILD)),
    ):
        await redis.xgroup_create(stream, "old-worker", id="0", mkstream=True)
    [(_stream, [(_id, ready)])] = await redis.xreadgroup(
        "old-worker", "old", {redis_queue.READY_STREAM_KEY: ">"}
    )
    assert ready[b"guild_id"] == str(GUILD).encode()
    [(_stream, wakes)] = await redis.xreadgroup(
        "old-worker", "old", {redis_queue.wake_stream_key(str(GUILD)): ">"}
    )
    assert [json.loads(fields[b"payload"])["channel_id"] for _id, fields in wakes] == [
        str(POST)
    ]


async def test_status_in_a_held_post_says_why(forum_setup, monkeypatch, moderator):
    setup = forum_setup({FORUM: True})
    _external(setup, monkeypatch, _Redis())
    ctx = _ctx(POST, whole_forum=False)

    await proactive.proactive_status.callback(ctx)

    body = ctx.respond.await_args.args[0]
    assert "**off**" in body and "can't answer forum posts yet" in body


# --- Discord lookups have a deadline --------------------------------------------


def _hanging_bot(started: list):
    async def hang(_channel_id):
        started.append(True)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            started.append("cancelled")
            raise

    return SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda _cid: None, get_thread=lambda _cid: None
        ),
        rest=SimpleNamespace(fetch_channel=AsyncMock(side_effect=hang)),
    )


async def test_a_stalled_parent_lookup_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(forum_posts, "LOOKUP_DEADLINE_SECONDS", 0.05)
    started: list = []
    parents = forum_posts.forum_parents(_hanging_bot(started))

    assert await asyncio.wait_for(parents.forum_for("11"), 1) is None
    assert await asyncio.wait_for(parents.is_forum("10"), 1) is None
    # The stalled requests were cancelled, and nothing was remembered.
    assert started.count("cancelled") == 2
    assert parents.known_forum_for("11") is None and "11" not in parents._parents


async def test_a_stalled_tag_lookup_falls_back_at_the_deadline(monkeypatch):
    monkeypatch.setattr(forum_posts, "LOOKUP_DEADLINE_SECONDS", 0.05)
    started: list = []

    names = await asyncio.wait_for(
        forum_posts.resolve_forum_tag_names(_hanging_bot(started), "10", [901]), 1
    )

    assert names == ["901"]
    assert "cancelled" in started


async def test_a_lookup_cancelled_from_outside_stays_cancelled(monkeypatch):
    started: list = []
    parents = forum_posts.forum_parents(_hanging_bot(started))

    task = asyncio.create_task(parents.forum_for("11"))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert "11" not in parents._parents


async def test_post_info_does_not_fetch_a_failed_forum_twice():
    post = SimpleNamespace(
        id=11,
        name="Q",
        type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
        parent_id=10,
        applied_tag_ids=[901],
    )
    forum_fetches = []

    async def fetch_channel(channel_id):
        if int(channel_id) == 11:
            return post
        forum_fetches.append(channel_id)
        raise RuntimeError("discord down")

    bot = SimpleNamespace(
        d={},
        cache=SimpleNamespace(
            get_guild_channel=lambda _cid: None, get_thread=lambda _cid: None
        ),
        rest=SimpleNamespace(fetch_channel=AsyncMock(side_effect=fetch_channel)),
    )

    info = await forum_posts.post_info(forum_posts.forum_parents(bot), "11", "10")

    assert info == forum_posts.ForumPostInfo(title="Q", tag_names=())
    assert len(forum_fetches) == 1
