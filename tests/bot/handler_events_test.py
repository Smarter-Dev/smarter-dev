"""Tests for the event-dispatch cheap-guard cache."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import hikari

from smarter_dev.bot.plugins import handler_events
from smarter_dev.bot.plugins.handler_events import (
    ActiveChannelsCache,
    _snowflake_created_at,
    author_has_manage_messages,
    dispatch_message,
    message_context,
    resolve_channel_parent_id,
)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAPI:
    def __init__(
        self,
        channels,
        guild_triggers=None,
        bot_message_channels=None,
        bot_message_guild_triggers=None,
    ):
        self.channels = channels
        self.guild_triggers = guild_triggers or []
        self.bot_message_channels = bot_message_channels or []
        self.bot_message_guild_triggers = bot_message_guild_triggers or []
        self.get_calls = 0

    async def get(self, path):
        self.get_calls += 1
        return _Resp({
            "channels": self.channels,
            "guild_triggers": self.guild_triggers,
            "bot_message_channels": self.bot_message_channels,
            "bot_message_guild_triggers": self.bot_message_guild_triggers,
        })


async def test_cache_reports_channel_membership():
    api = _FakeAPI([["C1", "message"], ["C2", "reaction"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "C1", "G1", "message") is True
    assert await cache.has(api, "C1", "G1", "reaction") is False
    assert await cache.has(api, "C9", "G1", "message") is False


async def test_cache_reports_guild_wide_admin_trigger():
    # An all-channel admin handler => every channel in that guild dispatches.
    api = _FakeAPI(channels=[], guild_triggers=[["G1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "ANY", "G1", "message") is True
    assert await cache.has(api, "ANY", "G2", "message") is False  # different guild
    assert await cache.has(api, "ANY", "G1", "reaction") is False


async def test_cache_avoids_refetch_within_ttl():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "G1", "message")
    await cache.has(api, "C1", "G1", "message")
    assert api.get_calls == 1


async def test_invalidate_forces_refetch():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "G1", "message")
    cache.invalidate()
    await cache.has(api, "C1", "G1", "message")
    assert api.get_calls == 2


def test_snowflake_created_at():
    # Discord snowflake -> ISO creation time (2015+).
    iso = _snowflake_created_at(733364234141827073)
    assert iso.startswith("20")  # a real UTC timestamp


# -- message trigger context enrichment (§3.1) ------------------------------

_AUTHOR_ID = "733364234141827073"


class _Author:
    def __init__(self, user_id: str = _AUTHOR_ID, username: str = "poster"):
        self.id = int(user_id)
        self.username = username


class _Message:
    def __init__(self, content: str = "hello", message_id: str = "M1"):
        self.content = content
        self.id = message_id
        self.author = _Author()


class _Role:
    def __init__(self, permissions: hikari.Permissions):
        self.permissions = permissions


class _Member:
    """Minimal member stand-in that ``lightbulb.utils.permissions_for`` accepts."""

    def __init__(self, roles: list[_Role], member_id: int = 1, owner: bool = False):
        self._roles = roles
        self.id = member_id
        self._owner = owner

    def get_roles(self):
        return self._roles

    def get_guild(self):
        return None  # no guild in cache -> owner short-circuit is skipped


def _base_context(**overrides) -> dict:
    kwargs = {
        "author_is_bot": False,
        "author_role_ids": [],
        "author_has_manage_messages": False,
        "mentioned_user_ids": [],
        "mentioned_role_ids": [],
        "mentions_everyone": False,
        "channel_parent_id": None,
        "author_joined_at": None,
        "attachments": [],
        "thread_fields": {"is_thread": False},
    }
    kwargs.update(overrides)
    return message_context(_Message(), **kwargs)


def test_message_context_includes_author_roles_and_permissions():
    context = _base_context(
        author_role_ids=["644390354157568014"],
        author_has_manage_messages=author_has_manage_messages(
            _Member([_Role(hikari.Permissions.MANAGE_MESSAGES)])
        ),
    )
    assert context["author_role_ids"] == ["644390354157568014"]
    assert context["author_has_manage_messages"] is True


def test_message_context_admin_permission_implies_manage_messages():
    # ADMINISTRATOR alone (no explicit MANAGE_MESSAGES) still reads as staff.
    assert (
        author_has_manage_messages(_Member([_Role(hikari.Permissions.ADMINISTRATOR)]))
        is True
    )
    # A plain member without either permission is NOT staff.
    assert (
        author_has_manage_messages(_Member([_Role(hikari.Permissions.SEND_MESSAGES)]))
        is False
    )


def test_message_context_fails_closed_when_member_uncached():
    # No cached member => scanned, never exempted: [] roles and NOT staff.
    assert author_has_manage_messages(None) is False
    context = _base_context(author_role_ids=[], author_has_manage_messages=False)
    assert context["author_role_ids"] == []
    assert context["author_has_manage_messages"] is False


def test_message_context_lists_mentioned_user_and_role_ids():
    context = _base_context(
        mentioned_user_ids=["111", "222"],
        mentioned_role_ids=["333"],
        mentions_everyone=True,
    )
    assert context["mentioned_user_ids"] == ["111", "222"]
    assert context["mentioned_role_ids"] == ["333"]
    assert context["mentions_everyone"] is True


@dataclass
class _FakeCache:
    guild_channels: dict = field(default_factory=dict)
    threads: dict = field(default_factory=dict)

    def get_guild_channel(self, channel_id):
        return self.guild_channels.get(int(channel_id))

    def get_thread(self, channel_id):
        return self.threads.get(int(channel_id))


@dataclass
class _FakeBot:
    cache: _FakeCache


class _Channel:
    def __init__(self, channel_type, parent_id):
        self.type = channel_type
        self.parent_id = parent_id


def test_message_context_channel_parent_id_is_category_for_top_level_channel():
    cache = _FakeCache(
        guild_channels={100: _Channel(hikari.ChannelType.GUILD_TEXT, 900)}
    )
    assert resolve_channel_parent_id(_FakeBot(cache), "100") == "900"


def test_message_context_channel_parent_id_none_on_cache_miss():
    assert resolve_channel_parent_id(_FakeBot(_FakeCache()), "100") is None


def test_message_context_thread_message_resolves_parent_category():
    # A message in a thread reports the thread's parent channel's category
    # (the grandparent), not the thread id.
    cache = _FakeCache(
        threads={50: _Channel(hikari.ChannelType.GUILD_PUBLIC_THREAD, 100)},
        guild_channels={100: _Channel(hikari.ChannelType.GUILD_TEXT, 900)},
    )
    assert resolve_channel_parent_id(_FakeBot(cache), "50") == "900"
    # Parent channel uncached => None (fail closed), never the thread id.
    thread_only = _FakeCache(
        threads={50: _Channel(hikari.ChannelType.GUILD_PUBLIC_THREAD, 100)}
    )
    assert resolve_channel_parent_id(_FakeBot(thread_only), "50") is None


def test_message_context_preserves_existing_fields():
    context = _base_context(
        author_joined_at="2026-01-01T00:00:00+00:00",
        attachments=[{"url": "u", "content_type": "image/png", "filename": "f"}],
        thread_fields={"is_thread": True, "thread_id": "T1", "thread_name": "chat"},
    )
    assert context["message_content"] == "hello"
    assert context["message_id"] == "M1"
    assert context["author_id"] == _AUTHOR_ID
    assert context["author_name"] == "poster"
    assert context["author_account_created_at"] == _snowflake_created_at(
        int(_AUTHOR_ID)
    )
    assert context["author_joined_at"] == "2026-01-01T00:00:00+00:00"
    assert context["attachments"][0]["filename"] == "f"
    assert context["is_thread"] is True
    assert context["thread_id"] == "T1"


# -- bot-message opt-in cache (has_bot_message) -----------------------------


async def test_active_channels_cache_has_bot_message():
    api = _FakeAPI(
        channels=[["C1", "message"]],
        bot_message_channels=["C1"],
        bot_message_guild_triggers=["G9"],
    )
    cache = ActiveChannelsCache(ttl_seconds=999)
    # True for a channel or a guild in the bot-message sets.
    assert await cache.has_bot_message(api, "C1", "G1") is True
    assert await cache.has_bot_message(api, "ANY", "G9") is True
    # False when neither the channel nor the guild opted in.
    assert await cache.has_bot_message(api, "C2", "G1") is False


class _FailAPI:
    async def get(self, path):
        raise RuntimeError("active-channels unavailable")


async def test_has_bot_message_false_on_refresh_failure():
    cache = ActiveChannelsCache(ttl_seconds=999)
    # A refresh failure never crashes dispatch — it reads as "not opted in".
    assert await cache.has_bot_message(_FailAPI(), "C1", "G1") is False


# -- dispatch_message bot-message routing -----------------------------------

_BOT_ME_ID = 999000111222333444


def _msg_bot_and_event(*, author_id, is_human):
    author = SimpleNamespace(id=author_id, username="poster")
    message = SimpleNamespace(
        id=1234,
        content="Bump done!",
        author=author,
        attachments=[],
        user_mentions_ids=[],
        role_mention_ids=[],
        mentions_everyone=False,
    )
    bot = SimpleNamespace(
        cache=_FakeCache(),
        get_me=lambda: SimpleNamespace(id=_BOT_ME_ID),
    )
    event = SimpleNamespace(
        is_human=is_human,
        guild_id=42,
        channel_id=555,
        message=message,
        member=None,
    )
    return bot, event


@dataclass
class _DispatchCapture:
    calls: list = field(default_factory=list)

    async def __call__(
        self, channel_id, guild_id, trigger_type, context, *, bot_message=False
    ):
        self.calls.append(
            {
                "channel_id": channel_id,
                "trigger_type": trigger_type,
                "context": context,
                "bot_message": bot_message,
            }
        )


async def test_dispatch_message_ignores_own_bot_message(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # A message authored by the bot itself never dispatches, human-looking or not.
    for is_human in (True, False):
        bot, event = _msg_bot_and_event(author_id=_BOT_ME_ID, is_human=is_human)
        await dispatch_message(bot, event)
    assert capture.calls == []


async def test_dispatch_message_bot_message_dispatches_when_opted_in(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # A foreign bot's message (is_human False, not our id) dispatches via the
    # bot-message route with author_is_bot True.
    bot, event = _msg_bot_and_event(author_id=302050872383242240, is_human=False)
    await dispatch_message(bot, event)
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["bot_message"] is True
    assert call["context"]["author_is_bot"] is True
    assert call["channel_id"] == "555"


async def test_dispatch_message_human_flags_not_bot_and_records_activity(monkeypatch):
    capture = _DispatchCapture()
    recorded: list = []
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    monkeypatch.setattr(
        handler_events._activity,
        "record",
        lambda g, u, at: recorded.append((g, u)),
    )
    bot, event = _msg_bot_and_event(author_id=733364234141827073, is_human=True)
    await dispatch_message(bot, event)
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["bot_message"] is False
    assert call["context"]["author_is_bot"] is False
    # A human message is recorded to the activity batcher.
    assert recorded == [("42", "733364234141827073")]


async def test_dispatch_message_bot_message_not_recorded_to_activity(monkeypatch):
    capture = _DispatchCapture()
    recorded: list = []
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    monkeypatch.setattr(
        handler_events._activity,
        "record",
        lambda g, u, at: recorded.append((g, u)),
    )
    bot, event = _msg_bot_and_event(author_id=302050872383242240, is_human=False)
    await dispatch_message(bot, event)
    # A bot/webhook is not a guild member — never recorded as activity.
    assert recorded == []


async def test_dispatch_message_bot_message_skipped_when_get_me_none(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # Pre-READY: get_me() is None, so the own-bot invariant can't be verified and
    # a bot message is dropped (fail closed).
    bot, event = _msg_bot_and_event(author_id=302050872383242240, is_human=False)
    bot.get_me = lambda: None
    await dispatch_message(bot, event)
    assert capture.calls == []
