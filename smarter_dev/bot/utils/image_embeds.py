"""Discord embed images, rendered by the internal media service.

Every card the bot posts is a 960x540 PNG drawn by ``services/media``. This
module is the thin Discord-facing wrapper: it turns a card request into a
``hikari.files.Bytes`` attachment whose bytes are fetched from the service when
hikari uploads it. Nothing is drawn in-process — there is no PIL fallback.

The generator methods stay synchronous so the ~100 command and view call sites
keep working unchanged. The HTTP request happens inside hikari's upload stream,
so nothing blocks the event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable

import hikari

from smarter_dev.bot.services.media_client import MediaClient
from smarter_dev.bot.services.media_client import RenderedMedia
from smarter_dev.shared.config import get_settings


class _LazyMediaBytes:
    """Fetches a rendered asset on first read and caches it for later reads.

    hikari accepts any async iterable of ``bytes`` as attachment data, so the
    render request is deferred until the attachment is actually uploaded.
    """

    def __init__(self, render: Callable[[], Awaitable[RenderedMedia]]) -> None:
        self._render = render
        self._data: bytes | None = None

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        if self._data is None:
            self._data = (await self._render()).data
        yield self._data


class EmbedImageGenerator:
    """Builds Discord attachments by asking the media service for the PNG."""

    def __init__(self, media_client: MediaClient) -> None:
        self._media_client = media_client

    @property
    def media_client(self) -> MediaClient:
        """The media service client this generator renders through."""
        return self._media_client

    def create_simple_embed(
        self,
        title: str,
        description: str,
        embed_type: str = "default",
    ) -> hikari.files.Bytes:
        """Create a simple embed with title and description."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_simple_embed(
                title=title, description=description, embed_type=embed_type
            ),
        )

    def create_error_embed(self, message: str) -> hikari.files.Bytes:
        """Create an error embed image."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_error_embed(message=message),
        )

    def create_success_embed(
        self, title: str, description: str
    ) -> hikari.files.Bytes:
        """Create a success embed image."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_success_embed(
                title=title, description=description
            ),
        )

    def create_info_embed(self, title: str, description: str) -> hikari.files.Bytes:
        """Create an info embed image."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_info_embed(
                title=title, description=description
            ),
        )

    def create_cooldown_embed(
        self,
        message: str,
        cooldown_end_timestamp: int | None = None,
    ) -> hikari.files.Bytes:
        """Create a cooldown-specific embed image."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_cooldown_embed(
                message=message, cooldown_end_timestamp=cooldown_end_timestamp
            ),
        )

    def create_leaderboard_embed(
        self,
        entries: list,
        guild_name: str,
        user_display_names: dict,
    ) -> hikari.files.Bytes:
        """Create a compact leaderboard embed image with table layout."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_leaderboard_embed(
                entries=entries,
                guild_name=guild_name,
                user_display_names=user_display_names,
            ),
        )

    def create_history_embed(
        self,
        transactions: list,
        user_id: str,
    ) -> hikari.files.Bytes:
        """Create a compact transaction history embed image with table layout."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_history_embed(
                transactions=transactions, user_id=user_id
            ),
        )

    def create_config_embed(self, config, guild_name: str) -> hikari.files.Bytes:
        """Create a compact configuration embed image with table layout."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_config_embed(
                config=config, guild_name=guild_name
            ),
        )

    def create_squad_list_embed(
        self,
        squads: list,
        guild_name: str,
        current_squad_id: str = None,
        guild_roles: dict[str, int] | None = None,
        has_active_campaign: bool = False,
    ) -> hikari.files.Bytes:
        """Create a compact squad list embed image with table layout."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_squad_list_embed(
                squads=squads,
                guild_name=guild_name,
                current_squad_id=current_squad_id,
                guild_roles=guild_roles,
                has_active_campaign=has_active_campaign,
            ),
        )

    def create_squad_info_embed(
        self,
        squad,
        members: list,
        user_member_info=None,
    ) -> hikari.files.Bytes:
        """Create a detailed squad information embed image."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_squad_info_embed(
                squad=squad, members=members, user_member_info=user_member_info
            ),
        )

    def create_squad_members_embed(
        self,
        squad,
        members: list,
    ) -> hikari.files.Bytes:
        """Create a squad members list embed image with table layout."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_squad_members_embed(
                squad=squad, members=members
            ),
        )

    def create_squad_join_selector_embed(
        self,
        user_balance: int,
        current_squad_name: str = None,
        available_squads_count: int = 0,
    ) -> hikari.files.Bytes:
        """Create a squad join selector embed image."""
        return self._attachment(
            "embed.png",
            lambda: self._media_client.create_squad_join_selector_embed(
                user_balance=user_balance,
                current_squad_name=current_squad_name,
                available_squads_count=available_squads_count,
            ),
        )

    def create_balance_embed(
        self,
        username: str,
        balance: int,
        streak_count: int = 0,
        last_daily: str | None = None,
        total_received: int = 0,
        total_sent: int = 0,
    ) -> hikari.files.Bytes:
        """Create a compact balance embed with table layout."""
        return self._attachment(
            "balance.png",
            lambda: self._media_client.create_balance_embed(
                username=username,
                balance=balance,
                streak_count=streak_count,
                last_daily=last_daily,
                total_received=total_received,
                total_sent=total_sent,
            ),
        )

    def create_transfer_success_embed(
        self,
        giver_name: str,
        receiver_name: str,
        amount: int,
        reason: str | None = None,
        new_balance: int | None = None,
    ) -> hikari.files.Bytes:
        """Create a transfer success embed image."""
        return self._attachment(
            "transfer_success.png",
            lambda: self._media_client.create_transfer_success_embed(
                giver_name=giver_name,
                receiver_name=receiver_name,
                amount=amount,
                reason=reason,
                new_balance=new_balance,
            ),
        )

    def _attachment(
        self,
        filename: str,
        render: Callable[[], Awaitable[RenderedMedia]],
    ) -> hikari.files.Bytes:
        """Wrap a deferred render in a hikari attachment."""
        return hikari.files.Bytes(_LazyMediaBytes(render), filename, "image/png")


_generator: EmbedImageGenerator | None = None


def get_generator() -> EmbedImageGenerator:
    """Get or create the process-wide image generator."""
    global _generator

    if _generator is None:
        _generator = EmbedImageGenerator(MediaClient.from_settings(get_settings()))

    return _generator


async def close_generator() -> None:
    """Dispose the generator's media client at shutdown."""
    global _generator

    generator = _generator
    _generator = None
    if generator is not None:
        await generator.media_client.aclose()
