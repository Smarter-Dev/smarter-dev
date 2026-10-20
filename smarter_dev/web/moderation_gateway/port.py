"""The gateway's only door to Discord.

Two implementations: the bot's (hikari, ``smarter_dev.bot.moderation_port``)
and the handler workers' (bot-token REST, :mod:`.rest_port`). The gateway
never sees either library.

Error contract for the effects (``ban`` … ``delete_messages``): raise
:class:`~.types.EffectRejected` (or :class:`~.types.EffectNotFound`) only when
Discord's answer proves nothing changed. Any other exception, cancellation
included, is an unknown outcome the gateway reconciles before trying again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from smarter_dev.web.models import ModerationAction
from smarter_dev.web.moderation_gateway.types import DeleteResult
from smarter_dev.web.moderation_gateway.types import TimeoutState


class ModerationPort(Protocol):
    """Discord as the gateway needs it, for one guild's actions."""

    async def bot_identity(self) -> tuple[str | None, str | None]:
        """The bot's own user id and name, for rows it writes as moderator."""
        ...

    async def staff_refusal(
        self, guild_id: str, user_id: str, *, is_bot: bool | None = None
    ) -> str | None:
        """The shared staff-protection rule; never raises."""
        ...

    async def timeout_state(self, guild_id: str, user_id: str) -> TimeoutState: ...

    async def is_banned(self, guild_id: str, user_id: str) -> bool: ...

    async def message_exists(self, channel_id: str, message_id: str) -> bool: ...

    async def message_author(
        self, channel_id: str, message_id: str
    ) -> tuple[str, str | None, bool] | None:
        """``(author id, name, is a bot or a webhook)``; None when it is gone."""
        ...

    async def ban(
        self, guild_id: str, user_id: str, *, reason: str, delete_message_seconds: int
    ) -> None: ...

    async def kick(self, guild_id: str, user_id: str, *, reason: str) -> None: ...

    async def set_timeout(
        self, guild_id: str, user_id: str, until: datetime | None, *, reason: str
    ) -> float | None:
        """Set (or with None, lift) the timeout; returns the stored expiry."""
        ...

    async def delete_messages(
        self, channel_id: str, message_ids: list[str]
    ) -> DeleteResult: ...

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        role_mention_ids: list[str] | None = None,
        attachment: tuple[str, bytes] | None = None,
    ) -> str:
        """Post with every mention off except ``role_mention_ids``; the new id."""
        ...

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        attachment: tuple[str, bytes] | None = None,
    ) -> None: ...

    async def find_message(self, channel_id: str, marker: str) -> str | None:
        """A recent message of the bot's in the channel containing ``marker``."""
        ...

    async def announce(self, action: ModerationAction) -> None:
        """Tell the observers (short-term memory, ``mod_action`` handlers)."""
        ...
