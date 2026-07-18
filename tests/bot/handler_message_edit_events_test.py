"""Tests for the admin-only message_edit trigger (pure builder + dispatch wiring).

message_edit fires on human message edits (hikari.GuildMessageUpdateEvent),
carrying the new content plus best-effort cached old content and the same
author-permission/category enrichment as the message trigger
(docs/v2/feature-parity/automated-and-command-moderation.md §3.3). The builder
and no-op-edit suppression are pure/bot-side so they are TDD-able without a
live gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot.plugins import handler_events
from smarter_dev.bot.plugins.handler_events import (
    _snowflake_created_at,
    dispatch_message_edit,
    message_edit_context,
)

# A real 2015+ Discord snowflake so account_created_at resolves sanely.
SNOWFLAKE = 733364234141827073
# The smarter-dev bot's own user id, distinct from the message author.
BOT_USER_ID = 999000111222333444


class _Role:
    def __init__(self, permissions: hikari.Permissions):
        self.permissions = permissions


class _EditMember:
    """Member stand-in accepted by both _roles_beyond_everyone and permissions_for."""

    def __init__(
        self,
        *,
        member_id=SNOWFLAKE,
        guild_id=42,
        role_ids=(42, 10),
        permissions=hikari.Permissions.MANAGE_MESSAGES,
        joined_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
    ):
        self.id = member_id
        self.guild_id = guild_id
        self.role_ids = tuple(role_ids)
        self._roles = [_Role(permissions)]
        self.joined_at = joined_at

    def get_roles(self):
        return self._roles

    def get_guild(self):
        return None  # no cached guild -> owner short-circuit skipped


class _Channel:
    def __init__(self, channel_type, parent_id):
        self.type = channel_type
        self.parent_id = parent_id


class FakeCache:
    def __init__(self, guild_channels=None, threads=None):
        self._guild_channels = guild_channels or {}
        self._threads = threads or {}

    def get_guild_channel(self, channel_id):
        return self._guild_channels.get(int(channel_id))

    def get_thread(self, channel_id):
        return self._threads.get(int(channel_id))


def make_edit_event(
    *,
    is_human=True,
    new_content="new text",
    old_content="old text",
    cached_old=True,
    channel_id=555,
    guild_id=42,
    thread_channel=None,
    member=None,
    guild_channels=None,
):
    author = SimpleNamespace(id=SNOWFLAKE, username="alice")
    message = SimpleNamespace(id=1234, content=new_content, author=author)
    old_message = (
        SimpleNamespace(content=old_content) if cached_old else None
    )
    channels = dict(guild_channels or {})
    threads = {}
    if thread_channel is not None:
        threads[int(channel_id)] = thread_channel
    bot = SimpleNamespace(cache=FakeCache(guild_channels=channels, threads=threads))
    event = SimpleNamespace(
        is_human=is_human,
        guild_id=guild_id,
        channel_id=channel_id,
        message=message,
        old_message=old_message,
        member=member,
    )
    return bot, event


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def fake_dispatch(
        channel_id, guild_id, trigger_type, context, *, bot_message=False
    ):
        calls.append((channel_id, guild_id, trigger_type, context))

    monkeypatch.setattr(handler_events, "_dispatch", fake_dispatch)
    return calls


# ---------------------------------------------------------------------------
# pure context builder
# ---------------------------------------------------------------------------


def test_message_edit_context_shape():
    msg = SimpleNamespace(
        id=1234,
        content="edited @everyone",
        author=SimpleNamespace(id=SNOWFLAKE, username="alice"),
    )
    ctx = message_edit_context(
        msg,
        old_content="was clean",
        author_role_ids=["10"],
        author_has_manage_messages=False,
        channel_parent_id="900",
        author_joined_at="2021-01-01T00:00:00+00:00",
        thread_fields={"is_thread": False},
    )
    assert ctx["trigger_type"] == "message_edit"
    assert ctx["message_id"] == "1234"
    assert ctx["message_content"] == "edited @everyone"  # what it says NOW
    assert ctx["old_content"] == "was clean"
    assert ctx["author_id"] == str(SNOWFLAKE)
    assert ctx["author_name"] == "alice"
    assert ctx["author_account_created_at"] == _snowflake_created_at(SNOWFLAKE)
    assert ctx["author_joined_at"] == "2021-01-01T00:00:00+00:00"
    assert ctx["author_role_ids"] == ["10"]
    assert ctx["author_has_manage_messages"] is False
    assert ctx["channel_parent_id"] == "900"
    assert ctx["is_thread"] is False


def test_message_edit_context_empty_content_falls_back_to_empty_string():
    msg = SimpleNamespace(
        id=1, content=None, author=SimpleNamespace(id=SNOWFLAKE, username="a")
    )
    ctx = message_edit_context(
        msg,
        old_content="",
        author_role_ids=[],
        author_has_manage_messages=False,
        channel_parent_id=None,
        author_joined_at=None,
        thread_fields={"is_thread": False},
    )
    assert ctx["message_content"] == ""
    assert ctx["old_content"] == ""


# ---------------------------------------------------------------------------
# dispatch wiring + no-op-edit suppression
# ---------------------------------------------------------------------------


async def test_message_edit_listener_dispatches_to_channel(captured):
    member = _EditMember(role_ids=(42, 10))
    bot, event = make_edit_event(
        member=member,
        guild_channels={555: _Channel(hikari.ChannelType.GUILD_TEXT, 900)},
    )
    await dispatch_message_edit(bot, event)
    assert len(captured) == 1
    channel_id, guild_id, trigger, ctx = captured[0]
    assert channel_id == "555"
    assert guild_id == "42"
    assert trigger == "message_edit"
    assert ctx["message_content"] == "new text"
    assert ctx["old_content"] == "old text"
    # context-rails enrichment resolved from the gateway cache.
    assert ctx["author_role_ids"] == ["10"]  # @everyone (guild id 42) filtered
    assert ctx["author_has_manage_messages"] is True
    assert ctx["channel_parent_id"] == "900"
    assert ctx["author_joined_at"] == "2021-01-01T00:00:00+00:00"


async def test_message_edit_listener_ignores_bot_edits(captured):
    # A bot/webhook edit (is_human False) never fires — no-loop invariant.
    bot, event = make_edit_event(is_human=False)
    await dispatch_message_edit(bot, event)
    assert captured == []


async def test_message_edit_listener_skips_unchanged_content(captured):
    # Discord re-emits the update for a link/embed unfurl with UNCHANGED text;
    # the cached old content equals the new content, so no fire.
    bot, event = make_edit_event(new_content="same", old_content="same")
    await dispatch_message_edit(bot, event)
    assert captured == []


async def test_message_edit_listener_skips_when_no_new_content(captured):
    # A pin/embed-only update carries UNDEFINED content — nothing to scan.
    bot, event = make_edit_event(new_content=hikari.UNDEFINED)
    await dispatch_message_edit(bot, event)
    assert captured == []


async def test_message_edit_listener_uncached_dispatches_with_empty_old(captured):
    # Old message not cached -> we cannot compare, so fire with old_content=""
    # (fail toward scanning — the auto-mod fail-closed stance).
    bot, event = make_edit_event(cached_old=False, member=None)
    await dispatch_message_edit(bot, event)
    assert len(captured) == 1
    _, _, trigger, ctx = captured[0]
    assert trigger == "message_edit"
    assert ctx["old_content"] == ""
    # No cached member -> scanned, never exempted.
    assert ctx["author_role_ids"] == []
    assert ctx["author_has_manage_messages"] is False


async def test_message_edit_in_thread_dispatches_to_parent(captured):
    # An edit inside a thread dispatches to the thread's PARENT channel (one fire),
    # so channel-scoped admin handlers catch edits exactly as they catch messages.
    thread = SimpleNamespace(
        id=999,
        name="side chat",
        parent_id=555,
        type=hikari.ChannelType.GUILD_PUBLIC_THREAD,
    )
    bot, event = make_edit_event(channel_id=999, thread_channel=thread)
    await dispatch_message_edit(bot, event)
    assert len(captured) == 1
    channel_id, _, trigger, ctx = captured[0]
    assert channel_id == "555"  # the parent, not the thread id
    assert ctx["is_thread"] is True
    assert ctx["thread_id"] == "999"
    assert ctx["thread_name"] == "side chat"
