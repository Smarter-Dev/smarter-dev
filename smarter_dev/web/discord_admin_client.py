"""Discord REST reads for the Skrift admin panel.

The admin guild pages need to show which guilds the bot is in and the details
of a single guild. Those are plain bot-token REST reads, so this client is a
thin subclass of :class:`~smarter_dev.web.discord_rest.DiscordBotClient` (the
same plumbing the worker tier uses) that adds the two admin endpoints and
shapes their JSON into small display records.

This replaces the read half of the legacy ``smarter_dev.web.admin.discord``
``DiscordClient`` that dies with the /bot-admin package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from smarter_dev.shared.config import get_settings
from smarter_dev.web.discord_rest import DiscordBotClient, DiscordRestError


class DiscordAdminError(DiscordRestError):
    """A Discord admin REST read failed."""


class GuildNotFoundError(DiscordAdminError):
    """The requested guild was not found or the bot is not a member."""


def _icon_url(guild_id: str, icon_hash: str | None) -> str | None:
    """Build the CDN URL for a guild icon, or ``None`` when unset."""
    if not icon_hash:
        return None
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png"


@dataclass(frozen=True)
class DiscordGuildSummary:
    """A guild as returned by the bot's guild list."""

    id: str
    name: str
    icon: str | None

    @property
    def icon_url(self) -> str | None:
        return _icon_url(self.id, self.icon)


@dataclass(frozen=True)
class DiscordGuildDetail:
    """Detailed information for a single guild."""

    id: str
    name: str
    icon: str | None
    owner_id: str
    member_count: int | None
    description: str | None

    @property
    def icon_url(self) -> str | None:
        return _icon_url(self.id, self.icon)


# Discord channel types that can receive squad announcement messages.
_ANNOUNCEMENT_CHANNEL_TYPES: frozenset[int] = frozenset({0, 5})


@dataclass(frozen=True)
class DiscordRole:
    """A guild role, shaped for the squad role picker."""

    id: str
    name: str
    color: int
    position: int
    managed: bool
    mentionable: bool

    @property
    def color_hex(self) -> str:
        """The role colour as a hex string, or Discord's default grey."""
        return f"#{self.color:06x}" if self.color else "#99aab5"


@dataclass(frozen=True)
class DiscordChannel:
    """A guild channel, shaped for the announcement-channel picker."""

    id: str
    name: str
    type: int
    position: int

    @property
    def supports_announcements(self) -> bool:
        """Whether this channel can receive squad announcement messages."""
        return self.type in _ANNOUNCEMENT_CHANNEL_TYPES


class DiscordAdminClient(DiscordBotClient):
    """Bot-token reads used by the admin guild pages."""

    user_agent: ClassVar[str] = "SmarterDev-Admin/1.0"
    error_type: ClassVar[type[DiscordAdminError]] = DiscordAdminError

    async def list_bot_guilds(self) -> list[DiscordGuildSummary]:
        """List every guild the bot is a member of."""
        response = await self._request("GET", "/users/@me/guilds")
        return [
            DiscordGuildSummary(
                id=guild["id"],
                name=guild["name"],
                icon=guild.get("icon"),
            )
            for guild in response.json()
        ]

    async def get_guild(self, guild_id: str) -> DiscordGuildDetail:
        """Fetch one guild's details.

        Raises:
            GuildNotFoundError: If the guild is unknown or the bot is not in it.
            DiscordAdminError: For any other Discord failure.
        """
        try:
            response = await self._request(
                "GET", f"/guilds/{guild_id}", params={"with_counts": "true"}
            )
        except DiscordAdminError as exc:
            if exc.status_code == 404:
                raise GuildNotFoundError(
                    f"Guild {guild_id} not found or bot is not a member"
                ) from exc
            raise

        guild = response.json()
        return DiscordGuildDetail(
            id=guild["id"],
            name=guild["name"],
            icon=guild.get("icon"),
            owner_id=guild["owner_id"],
            member_count=guild.get("approximate_member_count"),
            description=guild.get("description"),
        )

    async def get_guild_roles(self, guild_id: str) -> list[DiscordRole]:
        """List a guild's roles, ordered from highest position to lowest."""
        response = await self._request("GET", f"/guilds/{guild_id}/roles")
        roles = [
            DiscordRole(
                id=role["id"],
                name=role["name"],
                color=role["color"],
                position=role["position"],
                managed=role["managed"],
                mentionable=role["mentionable"],
            )
            for role in response.json()
        ]
        roles.sort(key=lambda role: role.position, reverse=True)
        return roles

    async def get_guild_channels(self, guild_id: str) -> list[DiscordChannel]:
        """List a guild's channels, ordered by position then name."""
        response = await self._request("GET", f"/guilds/{guild_id}/channels")
        channels = [
            DiscordChannel(
                id=channel["id"],
                name=channel["name"],
                type=channel["type"],
                position=channel.get("position", 0),
            )
            for channel in response.json()
        ]
        channels.sort(key=lambda channel: (channel.position, channel.name.lower()))
        return channels

    async def get_announcement_channels(
        self, guild_id: str
    ) -> list[DiscordChannel]:
        """List only the channels that can receive squad announcements."""
        channels = await self.get_guild_channels(guild_id)
        return [channel for channel in channels if channel.supports_announcements]


def get_admin_discord_client() -> DiscordAdminClient:
    """Build an admin Discord client from the configured bot token."""
    settings = get_settings()
    if not settings.discord_bot_token:
        raise DiscordAdminError("Discord bot token not configured")
    return DiscordAdminClient(bot_token=settings.discord_bot_token)
