"""Stripe webhook receiver for sudo membership events.

Verifies the ``Stripe-Signature`` header before dispatching to
``smarter_dev.web.billing.webhooks``. Returns 200 quickly so Stripe doesn't
re-queue events on every slow request.
"""

from __future__ import annotations

import logging
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.web.billing import webhooks as billing_webhooks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe-webhooks", tags=["Stripe Webhooks"])

SkriftSession = Annotated[AsyncSession, Depends(get_skrift_db_session)]


@router.post("/events", status_code=200)
async def stripe_webhook(
    request: Request,
    db_session: SkriftSession,
    stripe_signature: Annotated[str | None, Header(alias="stripe-signature")] = None,
) -> dict[str, str]:
    """Receive a Stripe webhook event, verify the signature, and dispatch."""
    settings = get_settings()
    webhook_secret = settings.stripe_webhook_secret
    if not webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET is not configured; rejecting event.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe webhooks are not configured.",
        )
    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe-Signature header.",
        )

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, webhook_secret
        )
    except ValueError:
        logger.exception("Malformed Stripe webhook payload.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payload.",
        )
    except stripe.error.SignatureVerificationError:
        logger.exception("Stripe webhook signature mismatch.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature.",
        )

    # Stripe SDK >=8 no longer inherits StripeObject from dict, so .get(...)
    # raises KeyError on the API objects. Convert the verified event to a
    # plain nested dict at the boundary — downstream handlers can use normal
    # dict semantics without caring about the SDK surface.
    event_dict = event.to_dict()

    try:
        await billing_webhooks.dispatch(db_session, event_dict)
    except Exception:
        logger.exception("Unhandled error dispatching Stripe event %s", event_dict.get("id"))
        # Bubble up as 500 so Stripe will retry — we'd rather receive the
        # event again than drop a paid checkout silently.
        raise

    return {"status": "ok"}
