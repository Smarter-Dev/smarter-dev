"""Tests for the admin-only member/thread event dispatch (pure functions + wiring).

Covers the five new admin-tier triggers (member_join, member_leave,
member_rules_accepted, member_role_change, thread_create) and thread-aware
message dispatch. Delta/context logic is pure-function bot-side so it is
TDD-able without a live gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot.plugins import handler_events
from smarter_dev.bot.plugins.handler_events import (
    _has_custom_avatar,
    dispatch_message,
    dispatch_thread_create,
    enrich_role_change_context,
    guild_member_counts,
    member_join_context,
    member_leave_context,
    member_update_deltas,
    message_thread_fields,
    on_member_join,
    on_member_leave,
    on_member_update,
    resolve_role_names,
    thread_create_context,
)

# A real 2015+ Discord snowflake so account_created_at resolves sanely.
SNOWFLAKE = 733364234141827073


def make_member(
    *,
    member_id=SNOWFLAKE,
    username="alice",
    display_name="Alice",
    nickname=None,
    is_bot=False,
    is_pending=False,
    role_ids=(),
    guild_id=None,
    avatar_hash="abc",
    guild_avatar_hash=None,
    joined_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
    premium_since=None,
):
    return SimpleNamespace(
        id=member_id,
        username=username,
        display_name=display_name,
        nickname=nickname,
        is_bot=is_bot,
        is_pending=is_pending,
        role_ids=tuple(role_ids),
        guild_id=guild_id,
        avatar_hash=avatar_hash,
        guild_avatar_hash=guild_avatar_hash,
        joined_at=joined_at,
        premium_since=premium_since,
    )


def make_user(*, user_id=SNOWFLAKE, username="alice", is_bot=False, avatar_hash="abc"):
    return SimpleNamespace(
        id=user_id,
        username=username,
        display_name=username.title(),
        is_bot=is_bot,
        avatar_hash=avatar_hash,
        guild_avatar_hash=None,
    )


# ---------------------------------------------------------------------------
# member_update_deltas — the pure delta/selection logic
# ---------------------------------------------------------------------------


def test_rules_accepted_fires_on_pending_transition():
    old = make_member(is_pending=True)
    new = make_member(is_pending=False)
    deltas = member_update_deltas(old, new)
    assert [t for t, _ in deltas] == ["member_rules_accepted"]
    _, ctx = deltas[0]
    assert ctx["trigger_type"] == "member_rules_accepted"
    assert ctx["member_id"] == str(SNOWFLAKE)


def test_rules_accepted_does_not_fire_when_still_pending():
    old = make_member(is_pending=True)
    new = make_member(is_pending=True)
    assert member_update_deltas(old, new) == []


def test_role_change_fires_with_added_and_removed_ids():
    old = make_member(role_ids=(10, 20))
    new = make_member(role_ids=(20, 30))
    deltas = member_update_deltas(old, new)
    assert [t for t, _ in deltas] == ["member_role_change"]
    _, ctx = deltas[0]
    assert ctx["added_role_ids"] == ["30"]
    assert ctx["removed_role_ids"] == ["10"]
    assert ctx["member_display_name"] == "Alice"


def test_rules_accepted_and_role_change_both_fire():
    old = make_member(is_pending=True, role_ids=())
    new = make_member(is_pending=False, role_ids=(30,))
    triggers = [t for t, _ in member_update_deltas(old, new)]
    assert set(triggers) == {"member_rules_accepted", "member_role_change"}


def test_no_deltas_when_nothing_changes():
    old = make_member(is_pending=False, role_ids=(10,))
    new = make_member(is_pending=False, role_ids=(10,))
    assert member_update_deltas(old, new) == []


def test_role_change_skips_on_cache_miss():
    # No old_member => no delta => no role_change fire (structural boost guard).
    new = make_member(role_ids=(10, 20))
    triggers = [t for t, _ in member_update_deltas(None, new)]
    assert "member_role_change" not in triggers


def test_rules_accepted_heuristic_fires_on_cache_miss_when_clean():
    # Cache miss: fire iff not pending AND no roles beyond @everyone (empty).
    new = make_member(is_pending=False, role_ids=())
    triggers = [t for t, _ in member_update_deltas(None, new)]
    assert triggers == ["member_rules_accepted"]


def test_rules_accepted_heuristic_skips_on_cache_miss_when_has_roles():
    new = make_member(is_pending=False, role_ids=(10,))
    assert member_update_deltas(None, new) == []


def test_rules_accepted_heuristic_skips_on_cache_miss_when_pending():
    new = make_member(is_pending=True, role_ids=())
    assert member_update_deltas(None, new) == []


def test_rules_accepted_heuristic_fires_when_only_everyone_role_held():
    # hikari always appends @everyone (id == guild id) to role_ids; a member who
    # just accepted the rules holds ONLY @everyone, so the heuristic must fire.
    new = make_member(is_pending=False, guild_id=42, role_ids=(42,))
    triggers = [t for t, _ in member_update_deltas(None, new)]
    assert triggers == ["member_rules_accepted"]


def test_rules_accepted_heuristic_skips_when_real_role_beyond_everyone():
    new = make_member(is_pending=False, guild_id=42, role_ids=(42, 10))
    assert member_update_deltas(None, new) == []


# ---------------------------------------------------------------------------
# context builders
# ---------------------------------------------------------------------------


def test_member_join_context_shape():
    member = make_member(is_bot=False)
    ctx = member_join_context(member, 12345, 11987)
    assert ctx["trigger_type"] == "member_join"
    assert ctx["member_id"] == str(SNOWFLAKE)
    assert ctx["is_bot"] is False
    assert ctx["has_custom_avatar"] is True
    assert ctx["guild_member_count"] == 12345
    assert ctx["guild_human_member_count"] == 11987
    assert ctx["account_created_at"].startswith("20")


def test_member_join_context_flags_bot():
    ctx = member_join_context(make_member(is_bot=True), 1, 0)
    assert ctx["is_bot"] is True


def test_member_leave_context_with_cached_old_member():
    user = make_user()
    old = make_member(role_ids=(10, 20))
    ctx = member_leave_context(user, old, ["Mod", "Helper"])
    assert ctx["trigger_type"] == "member_leave"
    assert ctx["cache_incomplete"] is False
    assert ctx["joined_at"] is not None
    assert ctx["role_ids"] == ["10", "20"]
    assert ctx["role_names"] == ["Mod", "Helper"]


def test_member_leave_context_excludes_everyone_role():
    # @everyone (id == guild id) must not leak into the reported role ids/names.
    user = make_user()
    old = make_member(guild_id=42, role_ids=(42, 10, 20))
    ctx = member_leave_context(user, old, ["Mod", "Helper"])
    assert ctx["role_ids"] == ["10", "20"]
    assert ctx["role_names"] == ["Mod", "Helper"]


def test_member_leave_context_on_cache_miss_is_partial():
    user = make_user()
    ctx = member_leave_context(user, None, [])
    assert ctx["cache_incomplete"] is True
    assert ctx["joined_at"] is None
    assert ctx["role_ids"] == []
    assert ctx["role_names"] == []
    # account_created_at always available from the snowflake.
    assert ctx["account_created_at"].startswith("20")


def test_has_custom_avatar():
    assert _has_custom_avatar(make_member(avatar_hash="x")) is True
    assert _has_custom_avatar(make_member(avatar_hash=None)) is False
    assert (
        _has_custom_avatar(make_member(avatar_hash=None, guild_avatar_hash="g")) is True
    )


# ---------------------------------------------------------------------------
# thread_create_context
# ---------------------------------------------------------------------------


def make_thread(
    *,
    thread_id=999,
    name="help me",
    parent_id=555,
    owner_id=SNOWFLAKE,
    applied_tag_ids=(),
    created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
):
    return SimpleNamespace(
        id=thread_id,
        name=name,
        parent_id=parent_id,
        owner_id=owner_id,
        applied_tag_ids=tuple(applied_tag_ids),
        created_at=created_at,
    )


def test_thread_create_context_forum_post():
    thread = make_thread(applied_tag_ids=(1, 2))
    creator = make_user(username="bob")
    ctx = thread_create_context(
        thread,
        creator,
        starter_message_content="how do I foo?",
        is_forum_post=True,
        applied_tag_names=["Question", "Python"],
    )
    assert ctx["trigger_type"] == "thread_create"
    assert ctx["thread_id"] == "999"
    assert ctx["parent_channel_id"] == "555"
    assert ctx["is_forum_post"] is True
    assert ctx["applied_tag_ids"] == ["1", "2"]
    assert ctx["applied_tag_names"] == ["Question", "Python"]
    assert ctx["starter_message_content"] == "how do I foo?"
    assert ctx["creator_username"] == "bob"


def test_thread_create_context_regular_thread():
    ctx = thread_create_context(
        make_thread(),
        make_user(),
        starter_message_content="",
        is_forum_post=False,
        applied_tag_names=[],
    )
    assert ctx["is_forum_post"] is False
    assert ctx["starter_message_content"] == ""
    assert ctx["applied_tag_names"] == []


# ---------------------------------------------------------------------------
# message thread fields
# ---------------------------------------------------------------------------


def test_message_thread_fields_non_thread():
    assert message_thread_fields(None) == {"is_thread": False}


def test_message_thread_fields_thread():
    thread = SimpleNamespace(id=999, name="side chat", parent_id=555)
    fields = message_thread_fields(thread)
    assert fields == {"is_thread": True, "thread_id": "999", "thread_name": "side chat"}


# ---------------------------------------------------------------------------
# role-change enrichment (guild-derived names/counts/boost)
# ---------------------------------------------------------------------------


def make_guild(*, roles=None, members=None, premium_subscription_count=0):
    roles = roles or {}
    members = members or {}

    def get_role(role_id):
        return roles.get(int(role_id))

    def get_members():
        return members

    return SimpleNamespace(
        get_role=get_role,
        get_members=get_members,
        premium_subscription_count=premium_subscription_count,
        member_count=len(members),
    )


def make_role(role_id, name, is_boost=False):
    return SimpleNamespace(
        id=role_id, name=name, is_premium_subscriber_role=is_boost
    )


def test_resolve_role_names_uses_guild_cache():
    guild = make_guild(roles={10: make_role(10, "Mod"), 20: make_role(20, "Helper")})
    assert resolve_role_names(guild, ["10", "20"]) == ["Mod", "Helper"]


def test_resolve_role_names_none_guild_is_empty():
    assert resolve_role_names(None, ["10"]) == []


def test_enrich_role_change_context_adds_names_and_boost():
    boost_role = make_role(30, "Server Booster", is_boost=True)
    booster = make_member(role_ids=(30,), premium_since=datetime.now(timezone.utc))
    guild = make_guild(
        roles={10: make_role(10, "Old"), 30: boost_role},
        members={1: booster},
        premium_subscription_count=14,
    )
    base = {
        "trigger_type": "member_role_change",
        "member_id": "1",
        "member_display_name": "Alice",
        "added_role_ids": ["30"],
        "removed_role_ids": ["10"],
    }
    ctx = enrich_role_change_context(base, None, None, guild)
    assert ctx["added_role_names"] == ["Server Booster"]
    assert ctx["removed_role_names"] == ["Old"]
    assert ctx["is_boost_role_added"] is True
    assert ctx["premium_subscription_count"] == 14
    assert ctx["boosting_member_count"] == 1
    assert ctx["role_member_counts"]["30"] == 1
    # pure: original dict is untouched
    assert "added_role_names" not in base


def test_guild_member_counts_counts_humans():
    members = {
        1: make_member(is_bot=False),
        2: make_member(is_bot=False),
        3: make_member(is_bot=True),
    }
    guild = make_guild(members=members)
    total, human = guild_member_counts(guild)
    assert total == 3
    assert human == 2


def test_guild_member_counts_none_guild():
    assert guild_member_counts(None) == (None, None)


# ---------------------------------------------------------------------------
# dispatch wiring — capture calls via monkeypatched _dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_dispatch(channel_id, guild_id, trigger_type, context):
        calls.append((channel_id, guild_id, trigger_type, context))

    monkeypatch.setattr(handler_events, "_dispatch", fake_dispatch)
    return calls


async def test_member_join_listener_dispatches_guild_scoped(captured):
    member = make_member()
    event = SimpleNamespace(
        member=member,
        guild_id=42,
        get_guild=lambda: make_guild(members={1: member}),
    )
    await on_member_join(event)
    assert len(captured) == 1
    channel_id, guild_id, trigger, ctx = captured[0]
    assert channel_id == ""  # member events have no home channel
    assert guild_id == "42"
    assert trigger == "member_join"


async def test_member_leave_listener_fires_on_cache_miss(captured):
    event = SimpleNamespace(
        user=make_user(),
        old_member=None,
        guild_id=42,
        get_guild=lambda: None,
    )
    await on_member_leave(event)
    assert len(captured) == 1
    _, _, trigger, ctx = captured[0]
    assert trigger == "member_leave"
    assert ctx["cache_incomplete"] is True


async def test_member_leave_listener_excludes_everyone_from_names(captured):
    old = make_member(guild_id=42, role_ids=(42, 10))
    guild = make_guild(
        roles={42: make_role(42, "@everyone"), 10: make_role(10, "Mod")}
    )
    event = SimpleNamespace(
        user=make_user(), old_member=old, guild_id=42, get_guild=lambda: guild
    )
    await on_member_leave(event)
    _, _, trigger, ctx = captured[0]
    assert trigger == "member_leave"
    assert ctx["role_ids"] == ["10"]
    assert ctx["role_names"] == ["Mod"]  # @everyone filtered before name resolve


async def test_member_update_listener_enriches_role_change(captured):
    old = make_member(role_ids=(10,))
    new = make_member(role_ids=(10, 30))
    guild = make_guild(
        roles={30: make_role(30, "VIP")},
        members={1: new},
        premium_subscription_count=3,
    )
    event = SimpleNamespace(
        old_member=old, member=new, guild_id=42, get_guild=lambda: guild
    )
    await on_member_update(event)
    assert len(captured) == 1
    channel_id, guild_id, trigger, ctx = captured[0]
    assert channel_id == ""
    assert trigger == "member_role_change"
    assert ctx["added_role_names"] == ["VIP"]


# ---------------------------------------------------------------------------
# thread_create dispatch
# ---------------------------------------------------------------------------


class FakeCache:
    def __init__(
        self, channels=None, threads=None, members=None, users=None, messages=None
    ):
        self._channels = channels or {}
        self._threads = threads or {}
        self._members = members or {}
        self._users = users or {}
        self._messages = messages or {}

    def get_guild_channel(self, channel_id):
        # Regular guild channels only — hikari's get_guild_channel never returns
        # threads, so a thread id here resolves to None (like production).
        return self._channels.get(int(channel_id))

    def get_thread(self, thread_id):
        return self._threads.get(int(thread_id))

    def get_member(self, guild_id, user_id):
        return self._members.get(int(user_id))

    def get_user(self, user_id):
        return self._users.get(int(user_id))

    def get_message(self, message_id):
        return self._messages.get(int(message_id))


def make_forum_channel(channel_id=555, tags=None):
    tags = tags or []
    return SimpleNamespace(
        id=channel_id,
        type=hikari.ChannelType.GUILD_FORUM,
        available_tags=tags,
    )


async def test_thread_create_only_fires_when_newly_created(captured):
    bot = SimpleNamespace(cache=FakeCache())
    event = SimpleNamespace(
        thread=make_thread(), guild_id=42, newly_created=False
    )
    await dispatch_thread_create(bot, event)
    assert captured == []


async def test_thread_create_dispatches_on_parent_channel(captured):
    parent = SimpleNamespace(id=555, type=hikari.ChannelType.GUILD_TEXT)
    bot = SimpleNamespace(cache=FakeCache(channels={555: parent}))
    event = SimpleNamespace(
        thread=make_thread(parent_id=555), guild_id=42, newly_created=True
    )
    await dispatch_thread_create(bot, event)
    assert len(captured) == 1
    channel_id, guild_id, trigger, ctx = captured[0]
    assert channel_id == "555"  # keyed off the PARENT channel
    assert trigger == "thread_create"
    assert ctx["is_forum_post"] is False


async def test_thread_create_forum_post_carries_tags_and_starter(captured):
    tag = SimpleNamespace(id=1, name="Question")
    forum = make_forum_channel(555, tags=[tag])
    starter = SimpleNamespace(content="how do I foo?")
    bot = SimpleNamespace(
        cache=FakeCache(channels={555: forum}, messages={999: starter})
    )
    event = SimpleNamespace(
        thread=make_thread(thread_id=999, parent_id=555, applied_tag_ids=(1,)),
        guild_id=42,
        newly_created=True,
    )
    await dispatch_thread_create(bot, event)
    _, _, _, ctx = captured[0]
    assert ctx["is_forum_post"] is True
    assert ctx["applied_tag_names"] == ["Question"]
    assert ctx["starter_message_content"] == "how do I foo?"


async def test_thread_create_forum_post_empty_starter_when_uncached(captured):
    forum = make_forum_channel(555)
    bot = SimpleNamespace(cache=FakeCache(channels={555: forum}))
    event = SimpleNamespace(
        thread=make_thread(thread_id=999, parent_id=555),
        guild_id=42,
        newly_created=True,
    )
    await dispatch_thread_create(bot, event)
    _, _, _, ctx = captured[0]
    assert ctx["is_forum_post"] is True
    assert ctx["starter_message_content"] == ""


# ---------------------------------------------------------------------------
# thread-aware message dispatch
# ---------------------------------------------------------------------------


def make_message_event(*, channel_id, thread_channel=None):
    author = SimpleNamespace(id=SNOWFLAKE, username="alice")
    message = SimpleNamespace(
        id=1234,
        content="hello",
        author=author,
        attachments=[],
        user_mentions_ids=[],
        role_mention_ids=[],
        mentions_everyone=False,
    )
    # A thread message's channel id resolves through cache.get_thread(), NOT
    # get_guild_channel() — so register the thread under threads (as production
    # does), proving the dispatch path reads threads from the right cache view.
    threads = {}
    if thread_channel is not None:
        threads[int(channel_id)] = thread_channel
    bot = SimpleNamespace(cache=FakeCache(threads=threads))
    event = SimpleNamespace(
        is_human=True,
        guild_id=42,
        channel_id=channel_id,
        message=message,
        member=None,
    )
    return bot, event


async def test_message_in_thread_dispatches_to_parent_only(captured):
    # §4: a single dispatch, keyed on the PARENT channel (home-channel semantics
    # unchanged) — never a second dispatch on the thread id.
    thread = SimpleNamespace(
        id=999,
        name="side chat",
        parent_id=555,
        type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
    )
    bot, event = make_message_event(channel_id=999, thread_channel=thread)
    await dispatch_message(bot, event)
    assert len(captured) == 1
    channel_id, _, trigger, ctx = captured[0]
    assert channel_id == "555"  # the parent, not the thread id
    assert trigger == "message"
    assert ctx["is_thread"] is True
    assert ctx["thread_id"] == "999"
    assert ctx["thread_name"] == "side chat"


async def test_non_thread_message_single_dispatch(captured):
    bot, event = make_message_event(channel_id=555, thread_channel=None)
    await dispatch_message(bot, event)
    assert len(captured) == 1
    channel_id, _, trigger, ctx = captured[0]
    assert channel_id == "555"
    assert ctx["is_thread"] is False
    assert "thread_id" not in ctx


async def test_message_from_bot_is_ignored(captured):
    bot, event = make_message_event(channel_id=555)
    event.is_human = False
    await dispatch_message(bot, event)
    assert captured == []
