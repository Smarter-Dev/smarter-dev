"""A fake Discord guild for moderation tests: cache, REST and a call log.

One :class:`FakeDiscord` stands in for the bot: ``bot.cache`` answers from the
guild it models, ``bot.rest`` answers the same facts over "REST" and records
every destructive call in ``destructive``. A test sets the guild up (roles,
channel overwrites, members, messages), runs the code under test, and reads
``destructive`` to see what it did.

``cached=False`` empties the cache so the REST fallbacks run; ``fail`` makes a
named REST method raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import hikari

GUILD_ID = 644299523686006834
BOT_ID = 1000
OWNER_ID = 1001


def not_found() -> hikari.NotFoundError:
    return hikari.NotFoundError(url="https://discord.test", headers={}, raw_body=b"")


def forbidden() -> hikari.ForbiddenError:
    return hikari.ForbiddenError(url="https://discord.test", headers={}, raw_body=b"")


def server_error() -> hikari.InternalServerError:
    return hikari.InternalServerError(
        url="https://discord.test", status=500, headers={}, raw_body=b""
    )


@dataclass
class FakeMember:
    id: int
    role_ids: list[int] = field(default_factory=list)
    is_bot: bool = False
    username: str = ""
    joined_at: datetime | None = None

    def __post_init__(self) -> None:
        self.raw_communication_disabled_until = None
        self.username = self.username or f"user{self.id}"
        self.display_name = self.username
        self.user = SimpleNamespace(
            id=self.id, is_bot=self.is_bot, fetch_dm_channel=self._dm_channel
        )

    async def _dm_channel(self):
        return 9_000_000 + self.id


@dataclass
class FakeChannel:
    id: int
    permission_overwrites: dict[int, Any] = field(default_factory=dict)


def overwrite(
    allow: hikari.Permissions = hikari.Permissions.NONE, deny=hikari.Permissions.NONE
):
    return SimpleNamespace(allow=allow, deny=deny)


class _FakeGuild:
    def __init__(self, discord: FakeDiscord) -> None:
        self._discord = discord
        self.id = discord.guild_id
        self.owner_id = discord.owner_id

    def get_roles(self):
        return self._discord._role_objects()

    def get_role(self, role_id):
        return self._discord._role_objects().get(int(role_id))

    def get_member(self, user_id):
        return self._discord.members.get(int(user_id))


class _FakeCache:
    def __init__(self, discord: FakeDiscord) -> None:
        self._discord = discord

    def _on(self) -> bool:
        return self._discord.cached

    def get_guild(self, guild_id):
        if not self._on() or int(guild_id) != self._discord.guild_id:
            return None
        return _FakeGuild(self._discord)

    def get_guild_channels_view_for_guild(self, guild_id):
        if not self._on():
            return {}
        return {channel.id: channel for channel in self._discord.channels}

    def get_member(self, guild_id, user_id):
        if not self._on():
            return None
        return self._discord.members.get(int(user_id))

    def get_user(self, user_id):
        if not self._on():
            return None
        member = self._discord.members.get(int(user_id))
        return member.user if member else self._discord.users.get(int(user_id))

    def get_role(self, role_id):
        if not self._on():
            return None
        return self._discord._role_objects().get(int(role_id))

    def get_guild_channel(self, channel_id):
        if not self._on():
            return None
        return next(
            (c for c in self._discord.channels if c.id == int(channel_id)), None
        )

    def get_thread(self, channel_id):
        return None


class _AsyncHistory:
    def __init__(self, messages: list) -> None:
        self._messages = messages

    def limit(self, n: int) -> _AsyncHistory:
        return _AsyncHistory(self._messages[:n])

    def __aiter__(self):
        async def gen():
            for message in self._messages:
                yield message

        return gen()


class _FakeRest:
    def __init__(self, discord: FakeDiscord) -> None:
        self._discord = discord

    def _maybe_fail(self, name: str) -> None:
        self._discord.reads.append(name)
        error = self._discord.fail.get(name)
        if error is not None:
            raise error

    # -- reads --
    async def fetch_member(self, guild_id, user_id):
        self._maybe_fail("fetch_member")
        member = self._discord.members.get(int(user_id))
        if member is None:
            raise not_found()
        return member

    async def fetch_guild(self, guild_id):
        self._maybe_fail("fetch_guild")
        return SimpleNamespace(
            id=self._discord.guild_id,
            owner_id=self._discord.owner_id,
            roles=self._discord._role_objects(),
        )

    async def fetch_roles(self, guild_id):
        self._maybe_fail("fetch_roles")
        return list(self._discord._role_objects().values())

    async def fetch_guild_channels(self, guild_id):
        self._maybe_fail("fetch_guild_channels")
        return list(self._discord.channels)

    async def fetch_user(self, user_id):
        self._maybe_fail("fetch_user")
        member = self._discord.members.get(int(user_id))
        if member is not None:
            return member.user
        user = self._discord.users.get(int(user_id))
        if user is None:
            raise not_found()
        return user

    async def fetch_message(self, channel_id, message_id):
        self._maybe_fail("fetch_message")
        for message in self._discord.messages:
            if int(message.id) == int(message_id):
                return message
        raise not_found()

    def fetch_messages(self, channel_id):
        self._discord.reads.append("fetch_messages")
        return _AsyncHistory(list(reversed(self._discord.messages)))

    async def fetch_ban(self, guild_id, user_id):
        self._maybe_fail("fetch_ban")
        if int(user_id) not in self._discord.bans:
            raise not_found()
        return SimpleNamespace(user=SimpleNamespace(id=int(user_id)))

    # -- writes --
    async def edit_member(self, guild_id, user_id, **kwargs):
        self._discord.destructive.append(("edit_member", int(user_id), kwargs))
        self._maybe_fail("edit_member")
        member = self._discord.members.get(int(user_id))
        if member is None:
            raise not_found()
        if "communication_disabled_until" in kwargs:
            member.raw_communication_disabled_until = kwargs["communication_disabled_until"]
        return member

    async def ban_user(self, guild_id, user_id, **kwargs):
        self._discord.destructive.append(("ban_user", int(user_id), kwargs))
        self._maybe_fail("ban_user")
        self._discord.bans.add(int(user_id))
        self._discord.members.pop(int(user_id), None)

    async def kick_user(self, guild_id, user_id, **kwargs):
        self._discord.destructive.append(("kick_user", int(user_id), kwargs))
        self._maybe_fail("kick_user")
        self._discord.members.pop(int(user_id), None)

    async def edit_message(self, channel, message, content=hikari.UNDEFINED, **kwargs):
        self._discord.edited.append((str(channel), str(message), content, kwargs))
        return SimpleNamespace(id=int(message))

    async def delete_message(self, channel_id, message_id, **kwargs):
        self._discord.destructive.append(("delete_message", int(message_id)))
        self._maybe_fail("delete_message")

    async def delete_messages(self, channel_id, message_ids, **kwargs):
        self._discord.destructive.append(
            ("delete_messages", [int(m) for m in message_ids])
        )
        self._maybe_fail("delete_messages")

    async def create_message(self, channel, content=hikari.UNDEFINED, **kwargs):
        self._discord.sent.append((str(channel), content, kwargs))
        return SimpleNamespace(id=999 + len(self._discord.sent))


class FakeDiscord:
    """The bot, as far as moderation code can tell."""

    def __init__(self, *, guild_id: int = GUILD_ID, owner_id: int = OWNER_ID) -> None:
        self.guild_id = guild_id
        self.owner_id = owner_id
        # role id -> permissions; @everyone's id is the guild's.
        self.roles: dict[int, hikari.Permissions] = {
            guild_id: hikari.Permissions.SEND_MESSAGES
        }
        self.channels: list[FakeChannel] = [FakeChannel(id=5000)]
        self.members: dict[int, FakeMember] = {}
        self.users: dict[int, Any] = {}
        self.messages: list = []
        self.cached = True
        self.fail: dict[str, Exception] = {}
        self.destructive: list[tuple] = []
        self.sent: list[tuple] = []
        self.edited: list[tuple] = []
        self.bans: set[int] = set()
        self.reads: list[str] = []
        self.cache = _FakeCache(self)
        self.rest = _FakeRest(self)
        self.add_member(BOT_ID, is_bot=True, username="smarter-dev")
        self.add_member(owner_id, username="owner")

    def get_me(self):
        return SimpleNamespace(id=BOT_ID, username="smarter-dev")

    def _role_objects(self) -> dict[int, Any]:
        return {
            role_id: SimpleNamespace(id=role_id, permissions=permissions)
            for role_id, permissions in self.roles.items()
        }

    def add_member(
        self, user_id: int, *, role_ids=(), is_bot: bool = False, **kw
    ) -> FakeMember:
        member = FakeMember(id=user_id, role_ids=list(role_ids), is_bot=is_bot, **kw)
        self.members[user_id] = member
        return member

    def add_message(
        self,
        message_id: int,
        author_id: int,
        *,
        content: str = "",
        is_bot=None,
        webhook_id=None,
    ):
        member = self.members.get(author_id)
        author_is_bot = (
            (member.is_bot if member else False) if is_bot is None else is_bot
        )
        message = SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(
                id=author_id, is_bot=author_is_bot, username=f"user{author_id}"
            ),
            content=content,
            webhook_id=webhook_id,
            created_at=datetime.now(UTC),
        )
        self.messages.append(message)
        return message
