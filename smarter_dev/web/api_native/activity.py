"""Native Litestar port of the member message-activity ingestion bot API.

Ports the legacy FastAPI ``routers/activity.py`` (prefix ``/activity``) — part
of unit U8 in docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact path,
verb, status code, and request/response shape of the FastAPI implementation so
``smarter_dev/bot/plugins/handler_events.py`` (and any external caller) needs
zero changes:

- ``POST /api/activity/batch`` → 200, ``{"recorded": <count>}``.

The bot reports every human guild message here in batches (one call per flush
interval, not per message); the data feeds the activity facts injected into
handler trigger contexts (see :mod:`smarter_dev.web.member_activity`). Rows land
in the Skrift DB (main database, ``skrift`` schema), which is exactly what the
Litestar-injected ``db_session`` targets — the legacy router reached the same DB
via ``get_skrift_db_session``.

Auth parity: the legacy endpoint required only a valid key (no scope gate), so
this port takes the base :data:`BOT_API_GUARDS` (``Permission("bot-api")``).
"""

from __future__ import annotations

from datetime import datetime

from litestar import Controller, post
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.member_activity import record_activity

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class ActivityEvent(BaseModel):
    guild_id: str
    user_id: str
    message_at: datetime


class ActivityBatchRequest(BaseModel):
    events: list[ActivityEvent] = Field(default_factory=list)


class ActivityController(Controller):
    """Member message-activity batch ingestion."""

    path = "/api/activity"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/batch", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def ingest_activity_batch(
        self,
        db_session: AsyncSession,
        data: ActivityBatchRequest,
    ) -> dict:
        """Upsert each reported message observation and commit once."""
        for event in data.events:
            await record_activity(
                db_session, event.guild_id, event.user_id, event.message_at
            )
        await db_session.commit()
        return {"recorded": len(data.events)}
