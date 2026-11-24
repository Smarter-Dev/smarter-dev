"""Bot API for code jams.

``GET /api/guilds/{guild_id}/jam`` tells the bot which jam is on and where its
channels are.


The bot polls ``POST /api/jam-messages/claim`` and posts what it gets back,
then reports each message with ``mark-sent`` or ``mark-failed``. Claiming
before posting is what makes a send at-most-once; see
:mod:`smarter_dev.web.jams.store`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import get
from litestar import post
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import Field
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.jams import store
from smarter_dev.web.models import JAM_CHANNEL_COLUMNS

# Declared per route, like the other bot API controllers: Skrift's auth guard
# looks for the ``APIKeyOnly`` marker on the route handler itself.
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission("bot-api")]


class MarkJamMessageFailedRequest(BaseModel):
    """Why the bot could not post a claimed message."""

    error: str = Field(min_length=1, max_length=2000)


def _parse_message_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as parse_error:
        raise ValidationException(detail="Invalid message_id format") from parse_error


class JamMessageController(Controller):
    """Claim and report scheduled jam messages."""

    path = "/api/jam-messages"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/claim", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def claim_due_jam_messages(
        self, db_session: AsyncSession
    ) -> dict[str, list[dict[str, Any]]]:
        """Claim every due message; each is returned to exactly one caller."""
        messages = await store.claim_due_messages(db_session)
        return {
            "messages": [
                {
                    "id": str(message.id),
                    "guild_id": message.guild_id,
                    "channel_id": message.channel_id,
                    "content": message.content,
                    "role_id": message.role_id,
                }
                for message in messages
            ]
        }

    @post("/{message_id:str}/mark-sent", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def mark_jam_message_sent(
        self, db_session: AsyncSession, message_id: str
    ) -> dict[str, bool]:
        """Record a claimed message as posted."""
        if not await store.mark_message_sent(db_session, _parse_message_id(message_id)):
            raise plain_error(404, "Jam message not found or not claimed")
        return {"success": True}

    @post(
        "/{message_id:str}/mark-failed", status_code=HTTP_200_OK, guards=BOT_API_GUARDS
    )
    async def mark_jam_message_failed(
        self,
        db_session: AsyncSession,
        message_id: str,
        data: MarkJamMessageFailedRequest,
    ) -> dict[str, bool]:
        """Record a claimed message as failed, with the bot's reason."""
        marked = await store.mark_message_failed(
            db_session, _parse_message_id(message_id), data.error
        )
        if not marked:
            raise plain_error(404, "Jam message not found or not claimed")
        return {"success": True}


class GuildJamController(Controller):
    """What the bot needs to know about a guild's jam."""

    path = "/api/guilds/{guild_id:str}/jam"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/", guards=BOT_API_GUARDS)
    async def get_guild_jam(
        self, db_session: AsyncSession, guild_id: str
    ) -> dict[str, Any]:
        """The guild's jam channels and the jam members can take part in now.

        ``jam`` is ``None`` between jams, and every id is ``None`` until setup
        has made that piece.
        """
        setup = await store.get_setup(db_session, guild_id)
        jam = await store.current_jam(db_session, guild_id)
        return {
            "channels": {
                key: setup.channel_id_for(key) if setup else None
                for key in JAM_CHANNEL_COLUMNS
            },
            "role_id": setup.role_id if setup else None,
            "jam": None
            if jam is None
            else {
                "id": str(jam.id),
                "name": jam.name,
                "max_team_size": jam.max_team_size,
                "starts_at": store.as_utc(jam.starts_at).isoformat(),
                "submissions_close_at": store.as_utc(
                    jam.submissions_close_at
                ).isoformat(),
                "showcase_tag_id": jam.showcase_tag_id,
                "year_tag_id": jam.year_tag_id,
            },
        }
