"""Native Litestar port of the billing-sensitive bot API (unit U10).

Ports the legacy FastAPI ``routers/polar_webhooks.py`` (prefix
``/polar-webhooks``) and ``routers/sudo_converge.py`` (prefix ``/sudo``) — unit
U10 in docs/v2/legacy-sunset/04-api-rewrite.md. This is the money path: the
port preserves signature verification and idempotency byte-for-byte.

- ``POST /api/polar-webhooks/events`` → 200 ``{"status": "ok"}``. **No API-key
  guard** — Polar authenticates each delivery with a standard-webhooks
  signature verified against ``settings.polar_webhook_secret`` via
  ``polar_sdk.webhooks.validate_event``, exactly as the legacy router did. The
  endpoint must remain reachable unauthenticated.
- ``POST /api/sudo/converge`` → 200 ``ConvergeResponse``. API-key
  authenticated (legacy required only a valid key, no scope gate, so this port
  takes the base :data:`BOT_API_GUARDS`). Called by the bot on
  ``GUILD_MEMBER_ADD`` (``smarter_dev/bot/client.py`` posts
  ``/sudo/converge``).

Idempotency (Polar delivers at-least-once): every verified delivery's
``webhook-id`` is inserted into ``webhook_events_processed`` and flushed BEFORE
dispatch; an ``IntegrityError`` on the flush means the delivery was already
processed — roll back and acknowledge with
``{"status": "ok", "duplicate": "true"}`` without ever running the side-effect
handler twice. A dispatch failure bubbles up as a 500 so Polar retries, and the
un-committed dedupe row is discarded with the session, allowing the retry to
attempt the handler again.

Commit parity: the legacy FastAPI session dependency (``get_skrift_db_session``)
committed after each successful handler. The Litestar-injected ``db_session``
does not, so both handlers commit explicitly on success — including after a
no-op dispatch (unknown event type), which persists the dedupe row exactly as
the legacy dependency did. Error-body parity for the 503/403/500 responses
comes from :func:`errors.plain_error` (bare ``{"detail": "<string>"}``).
"""

from __future__ import annotations

import logging
from uuid import UUID

from litestar import Controller, Request, post
from litestar.status_codes import HTTP_200_OK
from polar_sdk.webhooks import (
    WebhookVerificationError,
    validate_event,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.shared.config import get_settings
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    plain_error,
)
from smarter_dev.web.billing import webhooks as billing_webhooks
from smarter_dev.web.billing.converge import converge
from smarter_dev.web.models import WebhookEventProcessed

logger = logging.getLogger(__name__)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class PolarWebhookController(Controller):
    """Polar webhook receiver for sudo membership events.

    Signature-authenticated (standard-webhooks), NOT API-key authenticated —
    the route intentionally declares no guards.
    """

    path = "/api/polar-webhooks"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/events", status_code=HTTP_200_OK)
    async def receive_polar_event(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> dict[str, str]:
        """Receive a Polar webhook event, verify the signature, and dispatch."""
        settings = get_settings()
        webhook_secret = settings.polar_webhook_secret
        if not webhook_secret:
            logger.error("POLAR_WEBHOOK_SECRET is not configured; rejecting event.")
            raise plain_error(503, "Polar webhooks are not configured.")

        payload = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            event = validate_event(payload, headers, webhook_secret)
        except WebhookVerificationError:
            logger.exception("Polar webhook signature mismatch.")
            raise plain_error(403, "Invalid signature.")

        # Dedupe on the standard-webhooks delivery id: Polar delivers
        # at-least-once. If we've already processed this delivery, return 200
        # fast — never run the side-effect handler twice.
        event_id = headers.get("webhook-id")
        if event_id:
            record = WebhookEventProcessed(
                event_id=event_id, type=billing_webhooks.event_type(event) or ""
            )
            db_session.add(record)
            try:
                await db_session.flush()
            except IntegrityError:
                await db_session.rollback()
                logger.info(
                    "Polar event %s already processed; acknowledging duplicate.",
                    event_id,
                )
                return {"status": "ok", "duplicate": "true"}

        try:
            await billing_webhooks.dispatch(db_session, event)
        except Exception:
            logger.exception("Unhandled error dispatching Polar event %s", event_id)
            # Bubble up as 500 so Polar will retry — we'd rather receive the
            # event again than drop a paid order silently. The processed-row we
            # inserted above is never committed on this path, so the retry will
            # be allowed to attempt the handler again.
            raise

        # The legacy FastAPI session dependency committed here; this persists
        # the dedupe row even when dispatch no-ops for an unhandled event type
        # (the dispatch handlers commit their own writes).
        await db_session.commit()
        return {"status": "ok"}


class ConvergeRequest(BaseModel):
    discord_user_id: str = Field(..., description="Discord user ID to converge")


class ConvergeResponse(BaseModel):
    user_id: str | None = Field(None, description="Linked site user id, if any")
    added: list[str] = Field(default_factory=list, description="Role IDs added")
    removed: list[str] = Field(default_factory=list, description="Role IDs removed")
    linked: bool = Field(..., description="True if a site user was found")


class SudoConvergeController(Controller):
    """Internal sudo converge endpoint (bot-triggered role re-projection)."""

    path = "/api/sudo"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/converge", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def converge_by_discord(
        self,
        db_session: AsyncSession,
        data: ConvergeRequest,
    ) -> ConvergeResponse:
        """Trigger converge for whichever site user holds this Discord ID.

        No-op (still returns 200) if no site account is linked yet — the next
        trigger (link, member-add, daily sweep) heals.
        """
        result = await db_session.execute(
            text(
                "SELECT user_id FROM skrift.oauth_accounts "
                "WHERE provider = 'discord' AND provider_account_id = :did "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"did": data.discord_user_id},
        )
        row = result.first()
        if row is None:
            return ConvergeResponse(user_id=None, linked=False)

        user_id = UUID(str(row[0]))
        try:
            outcome = await converge(db_session, user_id)
        except Exception:
            logger.exception(
                "converge endpoint: unexpected failure for user %s", user_id
            )
            raise plain_error(500, "converge failed")
        # The legacy FastAPI session dependency committed converge's writes
        # after the handler returned; the Litestar-injected session needs the
        # explicit commit.
        await db_session.commit()
        return ConvergeResponse(
            user_id=str(user_id),
            linked=True,
            added=outcome["added"],
            removed=outcome["removed"],
        )
