"""Shared builders for the jam tests."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import DiscordChannel
from smarter_dev.web.discord_admin_client import DiscordRole
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamDiscordSetup
from smarter_dev.web.models import JamMessage

GUILD = "111111111111111111"
BOT_USER = "999999999999999999"
START = datetime(2026, 12, 1, tzinfo=UTC)


def make_jam(**overrides) -> Jam:
    values = {
        "guild_id": GUILD,
        "name": "Holiday Chatbot Jam",
        "tag_name": "Holiday Jam",
        "max_team_size": 4,
        "starts_at": START,
        "submissions_close_at": START + timedelta(days=33),
        "created_by": "admin",
    }
    values.update(overrides)
    return Jam(**values)


def make_setup(**overrides) -> JamDiscordSetup:
    values = {
        "guild_id": GUILD,
        "category_id": "10",
        "news_channel_id": "11",
        "chat_channel_id": "12",
        "showcase_channel_id": "14",
        "role_id": "20",
    }
    values.update(overrides)
    return JamDiscordSetup(**values)


def make_message(jam: Jam, **overrides) -> JamMessage:
    values = {
        "jam_id": jam.id,
        "channel_key": "news",
        "content": "The jam is open!",
        "mention_role": False,
        "send_at": START,
        "created_by": "admin",
    }
    values.update(overrides)
    return JamMessage(**values)


def discord_error(status: int | None) -> DiscordAdminError:
    error = DiscordAdminError(f"request failed: {status}")
    error.status_code = status
    return error


class FakeDiscord:
    """An in-memory guild that answers the calls jam setup makes."""

    def __init__(self) -> None:
        self.channels: list[DiscordChannel] = []
        self.roles: list[DiscordRole] = []
        self.tags: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        # Method name -> error to raise instead of answering.
        self.fail: dict[str, DiscordAdminError] = {}
        self._next_id = 1000

    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def _check(self, method: str) -> None:
        if method in self.fail:
            raise self.fail[method]

    def add_channel(self, name: str, kind: int, parent_id: str | None = None) -> str:
        channel = DiscordChannel(
            id=self._id(), name=name, type=kind, position=0, parent_id=parent_id
        )
        self.channels.append(channel)
        if kind == 15:
            self.tags[channel.id] = []
        return channel.id

    def add_role(self, name: str, permissions: int = 0, managed: bool = False) -> str:
        role = DiscordRole(
            id=self._id(),
            name=name,
            color=0,
            position=1,
            managed=managed,
            mentionable=False,
            permissions=permissions,
        )
        self.roles.append(role)
        return role.id

    def add_tag(self, forum_id: str, name: str) -> str:
        tag = {"id": self._id(), "name": name, "moderated": False}
        self.tags[forum_id].append(tag)
        return tag["id"]

    def created(self, method: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == method]

    async def get_guild_channels(self, guild_id: str) -> list[DiscordChannel]:
        self._check("get_guild_channels")
        return list(self.channels)

    async def get_guild_roles(self, guild_id: str) -> list[DiscordRole]:
        self._check("get_guild_roles")
        return list(self.roles)

    async def get_bot_user_id(self) -> str:
        self._check("get_bot_user_id")
        return BOT_USER

    async def create_guild_channel(
        self, guild_id: str, payload: dict, *, reason: str
    ) -> DiscordChannel:
        self._check(f"create_guild_channel:{payload['name']}")
        self._check("create_guild_channel")
        self.calls.append(("create_guild_channel", payload, reason))
        channel_id = self.add_channel(
            payload["name"], payload["type"], payload.get("parent_id")
        )
        return self.channels[-1] if self.channels[-1].id == channel_id else None

    async def create_guild_role(
        self, guild_id: str, name: str, *, reason: str
    ) -> DiscordRole:
        self._check("create_guild_role")
        self.calls.append(("create_guild_role", name, reason))
        self.add_role(name)
        return self.roles[-1]

    async def get_forum_tags(self, channel_id: str) -> list[dict]:
        self._check("get_forum_tags")
        return [dict(tag) for tag in self.tags[channel_id]]

    async def set_forum_tags(
        self, channel_id: str, tags: list[dict], *, reason: str
    ) -> list[dict]:
        self._check("set_forum_tags")
        self.calls.append(("set_forum_tags", channel_id, tags, reason))
        self.tags[channel_id] = [
            tag if "id" in tag else {**tag, "id": self._id()} for tag in tags
        ]
        return [dict(tag) for tag in self.tags[channel_id]]
