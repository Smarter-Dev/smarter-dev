"""Tests for the event-dispatch cheap-guard cache."""

from __future__ import annotations

from dataclasses import dataclass, field

import hikari

from smarter_dev.bot.plugins.handler_events import (
    ActiveChannelsCache,
    _snowflake_created_at,
    author_has_manage_messages,
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
    def __init__(self, channels, guild_triggers=None):
        self.channels = channels
        self.guild_triggers = guild_triggers or []
        self.get_calls = 0

    async def get(self, path):
        self.get_calls += 1
        return _Resp({"channels": self.channels, "guild_triggers": self.guild_triggers})


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
