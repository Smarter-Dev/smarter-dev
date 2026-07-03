"""Polar webhook receiver for sudo membership events.

Verifies the standard-webhooks signature before dispatching to
``smarter_dev.web.billing.webhooks``. Returns 200 quickly so Polar doesn't
re-queue events on every slow request.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from polar_sdk.webhooks import (
    WebhookVerificationError,
    validate_event,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.web.billing import webhooks as billing_webhooks
from smarter_dev.web.models import WebhookEventProcessed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/polar-webhooks", tags=["Polar Webhooks"])

SkriftSession = Annotated[AsyncSession, Depends(get_skrift_db_session)]


@router.post("/events", status_code=200)
async def polar_webhook(
    request: Request,
    db_session: SkriftSession,
) -> dict[str, str]:
    """Receive a Polar webhook event, verify the signature, and dispatch."""
    settings = get_settings()
    webhook_secret = settings.polar_webhook_secret
    if not webhook_secret:
        logger.error("POLAR_WEBHOOK_SECRET is not configured; rejecting event.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Polar webhooks are not configured.",
        )

    payload = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        event = validate_event(payload, headers, webhook_secret)
    except WebhookVerificationError:
        logger.exception("Polar webhook signature mismatch.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid signature.",
        )

    # Dedupe on the standard-webhooks delivery id: Polar delivers at-least-once.
    # If we've already processed this delivery, return 200 fast — never run the
    # side-effect handler twice.
    event_id = headers.get("webhook-id")
    if event_id:
        record = WebhookEventProcessed(event_id=event_id, type=event.type)
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
        # Bubble up as 500 so Polar will retry — we'd rather receive the event
        # again than drop a paid order silently. The processed-row we inserted
        # above rolls back with the session on the 500, so the retry will be
        # allowed to attempt the handler again.
        raise

    return {"status": "ok"}
