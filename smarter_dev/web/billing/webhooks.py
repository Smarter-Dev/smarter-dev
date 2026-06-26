"""Stripe webhook event handling for sudo memberships.

The webhook handler is the source of truth for role grants and membership
lifecycle. Every handler is idempotent and keyed by ``stripe_checkout_session_id``
/ ``stripe_payment_intent_id`` / ``stripe_subscription_id`` so Stripe's
automatic retries are safe.

Two offerings, two lifecycles:

* **founder** — ``checkout.session.completed`` in ``mode=payment``. One-time,
  permanent access (``expires_at`` set far in the future). ``charge.refunded``
  / ``charge.dispute.created`` revoke it.
* **hacker** — ``checkout.session.completed`` in ``mode=subscription`` creates
  the membership with ``expires_at`` = the subscription's current period end.
  ``invoice.paid`` extends it each renewal; ``customer.subscription.updated``
  tracks ``cancel_at_period_end``; ``customer.subscription.deleted`` ends access.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.services import assign_role_to_user, remove_role_from_user

from smarter_dev.web.billing.client import get_stripe
from smarter_dev.web.billing.converge import converge
from smarter_dev.web.models import SudoMembership

logger = logging.getLogger(__name__)


# Founder is one-time and permanent; we model that as a far-future expiry so the
# same "active = not revoked and expires_at > now" predicate works for both.
FOUNDER_ACCESS_END = datetime(9999, 12, 31, tzinfo=timezone.utc)

# Map an offering role to the Skrift role it grants.
ROLE_TO_SKRIFT: dict[str, str] = {
    "hacker": "sudo-hacker",
    "founder": "sudo-founder",
}


async def _get_by_session_id(
    session: AsyncSession, checkout_session_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.stripe_checkout_session_id == checkout_session_id
        )
    )
    return result.scalar_one_or_none()


async def _get_by_payment_intent(
    session: AsyncSession, payment_intent_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.stripe_payment_intent_id == payment_intent_id
        )
    )
    return result.scalar_one_or_none()


async def _get_by_subscription_id(
    session: AsyncSession, subscription_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.stripe_subscription_id == subscription_id
        )
    )
    return result.scalar_one_or_none()


async def _grant_role(session: AsyncSession, user_id: UUID, role: str) -> None:
    role_name = ROLE_TO_SKRIFT.get(role)
    if not role_name:
        logger.warning("No Skrift role mapping for %r; skipping grant.", role)
        return
    assigned = await assign_role_to_user(session, user_id, role_name)
    if not assigned:
        logger.error(
            "Failed to assign role %s to user %s (user or role not found).",
            role_name, user_id,
        )


async def _revoke_role(session: AsyncSession, user_id: UUID, role: str) -> None:
    role_name = ROLE_TO_SKRIFT.get(role)
    if not role_name:
        return
    await remove_role_from_user(session, user_id, role_name)


def _subscription_period_end(sub: dict[str, Any]) -> datetime:
    """Best-effort current period end for a subscription dict → aware datetime.

    Stripe moved ``current_period_end`` onto subscription items in newer API
    versions; read the top level first, then fall back to the first item.
    """
    ts = sub.get("current_period_end")
    if not ts:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            ts = items[0].get("current_period_end")
    if not ts:
        # No period info; default to "now" so a stale sub doesn't grant forever.
        return datetime.now(tz=timezone.utc)
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


async def _converge_quietly(session: AsyncSession, user_id: UUID, where: str) -> None:
    try:
        await converge(session, user_id)
    except Exception:
        logger.exception("converge failed after %s for user %s", where, user_id)


async def handle_checkout_session_completed(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Create the membership for a completed checkout (founder or hacker)."""
    session_obj = event_data["object"]
    checkout_session_id = session_obj.get("id")
    user_id_raw = session_obj.get("client_reference_id")
    if not user_id_raw:
        logger.error(
            "checkout.session.completed without client_reference_id; session=%s",
            checkout_session_id,
        )
        return

    user_id = UUID(user_id_raw)
    metadata = session_obj.get("metadata") or {}
    role = metadata.get("role")
    if role not in ROLE_TO_SKRIFT:
        logger.error(
            "checkout.session.completed with unknown role %r; session=%s",
            role, checkout_session_id,
        )
        return

    stripe_customer_id = session_obj.get("customer")
    if not stripe_customer_id:
        logger.error(
            "checkout.session.completed missing customer; session=%s",
            checkout_session_id,
        )
        return

    # Idempotency: if we've already recorded this checkout, do nothing.
    if await _get_by_session_id(session, checkout_session_id) is not None:
        return

    amount_paid_cents = int(session_obj.get("amount_total") or 0)
    mode = session_obj.get("mode")
    stripe = get_stripe()

    if mode == "subscription":
        subscription_id = session_obj.get("subscription")
        if not subscription_id:
            logger.error(
                "subscription checkout missing subscription id; session=%s",
                checkout_session_id,
            )
            return
        import json

        sub = json.loads(str(stripe.Subscription.retrieve(subscription_id)))
        items = (sub.get("items") or {}).get("data") or []
        price_id = items[0]["price"]["id"] if items else ""
        expires_at = _subscription_period_end(sub)
        will_renew = not bool(sub.get("cancel_at_period_end"))
        membership = SudoMembership(
            user_id=user_id,
            role=role,
            source="subscription",
            stripe_customer_id=stripe_customer_id,
            stripe_checkout_session_id=checkout_session_id,
            stripe_subscription_id=subscription_id,
            stripe_price_id=price_id,
            amount_paid_cents=amount_paid_cents,
            will_renew=will_renew,
            expires_at=expires_at,
        )
    else:  # one-time payment (founder)
        payment_intent_id = session_obj.get("payment_intent")
        if not payment_intent_id:
            logger.error(
                "payment checkout missing payment_intent; session=%s",
                checkout_session_id,
            )
            return
        line_items = stripe.checkout.Session.list_line_items(checkout_session_id, limit=1)
        price_id = line_items["data"][0]["price"]["id"] if line_items["data"] else ""
        membership = SudoMembership(
            user_id=user_id,
            role=role,
            source="one_time",
            stripe_customer_id=stripe_customer_id,
            stripe_checkout_session_id=checkout_session_id,
            stripe_payment_intent_id=payment_intent_id,
            stripe_price_id=price_id,
            amount_paid_cents=amount_paid_cents,
            expires_at=FOUNDER_ACCESS_END,
        )

    session.add(membership)
    await session.commit()

    await _grant_role(session, user_id, role)
    await session.commit()
    await _converge_quietly(session, user_id, "fulfillment")


async def handle_invoice_paid(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Extend a hacker membership's access window on each paid invoice."""
    invoice = event_data["object"]
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return
    record = await _get_by_subscription_id(session, subscription_id)
    if record is None:
        # The creating checkout.session.completed hasn't landed yet; it will
        # set expires_at from the subscription. Nothing to do.
        return

    import json

    stripe = get_stripe()
    sub = json.loads(str(stripe.Subscription.retrieve(subscription_id)))
    new_end = _subscription_period_end(sub)
    changed = False
    if record.expires_at != new_end:
        record.expires_at = new_end
        changed = True
    will_renew = not bool(sub.get("cancel_at_period_end"))
    if record.will_renew != will_renew:
        record.will_renew = will_renew
        changed = True
    # A previously-lapsed/canceled sub that pays again should be live.
    if record.revoked_reason is None and changed:
        await session.commit()
        await _grant_role(session, record.user_id, record.role)
        await session.commit()
        await _converge_quietly(session, record.user_id, "invoice.paid")


async def handle_subscription_updated(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Track ``cancel_at_period_end`` and period end for a hacker membership."""
    sub = event_data["object"]
    subscription_id = sub.get("id")
    if not subscription_id:
        return
    record = await _get_by_subscription_id(session, subscription_id)
    if record is None:
        return
    record.will_renew = not bool(sub.get("cancel_at_period_end"))
    record.expires_at = _subscription_period_end(sub)
    await session.commit()


async def handle_subscription_deleted(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """End access when a hacker subscription is fully canceled."""
    sub = event_data["object"]
    subscription_id = sub.get("id")
    if not subscription_id:
        return
    record = await _get_by_subscription_id(session, subscription_id)
    if record is None:
        return

    record.will_renew = False
    record.expires_at = datetime.now(tz=timezone.utc)
    await session.commit()

    await _revoke_role(session, record.user_id, record.role)
    await session.commit()
    await _converge_quietly(session, record.user_id, "subscription.deleted")


async def handle_charge_refunded(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Handle ``charge.refunded`` for a founder purchase — revoke access."""
    charge = event_data["object"]
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return

    record = await _get_by_payment_intent(session, payment_intent_id)
    if record is None:
        logger.info(
            "charge.refunded for unknown payment_intent %s; ignoring.",
            payment_intent_id,
        )
        return
    if record.refunded_at is not None:
        return

    record.refunded_at = datetime.now(tz=timezone.utc)
    record.revoked_reason = "refund"
    await session.commit()

    await _revoke_role(session, record.user_id, record.role)
    await session.commit()
    await _converge_quietly(session, record.user_id, "refund")


async def handle_charge_dispute_created(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Handle ``charge.dispute.created`` — clamp access immediately."""
    dispute = event_data["object"]
    payment_intent_id = dispute.get("payment_intent")
    if not payment_intent_id:
        return

    record = await _get_by_payment_intent(session, payment_intent_id)
    if record is None:
        logger.info(
            "charge.dispute.created for unknown payment_intent %s; ignoring.",
            payment_intent_id,
        )
        return
    if record.revoked_reason == "dispute":
        return

    record.revoked_reason = "dispute"
    record.expires_at = datetime.now(tz=timezone.utc)
    await session.commit()

    await _revoke_role(session, record.user_id, record.role)
    await session.commit()
    await _converge_quietly(session, record.user_id, "dispute")


async def handle_charge_dispute_closed(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Handle ``charge.dispute.closed`` — log only; restores are manual."""
    dispute = event_data["object"]
    logger.info(
        "charge.dispute.closed status=%s payment_intent=%s — manual review.",
        dispute.get("status"), dispute.get("payment_intent"),
    )


async def expire_lapsed_memberships(session: AsyncSession) -> int:
    """Revoke roles for any memberships whose ``expires_at`` has passed."""
    now = datetime.now(tz=timezone.utc)
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.expires_at < now,
            SudoMembership.refunded_at.is_(None),
            SudoMembership.revoked_reason.is_(None),
        )
    )
    expired = 0
    expired_user_ids: list[UUID] = []
    for membership in result.scalars():
        role_name = ROLE_TO_SKRIFT.get(membership.role)
        if role_name is None:
            continue
        await remove_role_from_user(session, membership.user_id, role_name)
        expired_user_ids.append(membership.user_id)
        expired += 1
    if expired:
        await session.commit()
    for uid in expired_user_ids:
        await _converge_quietly(session, uid, "sweep")
    return expired


async def drift_restore_active(session: AsyncSession) -> int:
    """Re-converge every active membership (heals manual/role drift)."""
    now = datetime.now(tz=timezone.utc)
    result = await session.execute(
        select(SudoMembership.user_id)
        .where(SudoMembership.expires_at > now)
        .where(SudoMembership.revoked_reason.is_(None))
        .distinct()
    )
    user_ids = [row[0] for row in result.all()]
    for uid in user_ids:
        await _converge_quietly(session, uid, "drift-restore")
    return len(user_ids)


async def run_daily_sweep(session: AsyncSession) -> dict[str, Any]:
    """Expire lapsed memberships + drift-restore active ones.

    Renewal reminders are gone: Hacker is a Stripe subscription (Stripe owns
    dunning) and Founder is permanent, so there's nothing to remind about.
    """
    expired = await expire_lapsed_memberships(session)
    converged = await drift_restore_active(session)
    logger.info(
        "sudo daily sweep done: expired=%d, drift-restored=%d",
        expired, converged,
    )
    return {"expired": expired, "drift_restored": converged}


_HANDLERS = {
    "checkout.session.completed": handle_checkout_session_completed,
    # Async-payment methods (ACH etc.) deliver this when the funds clear.
    "checkout.session.async_payment_succeeded": handle_checkout_session_completed,
    "invoice.paid": handle_invoice_paid,
    "customer.subscription.updated": handle_subscription_updated,
    "customer.subscription.deleted": handle_subscription_deleted,
    "charge.refunded": handle_charge_refunded,
    "charge.dispute.created": handle_charge_dispute_created,
    "charge.dispute.closed": handle_charge_dispute_closed,
}


async def dispatch(session: AsyncSession, event: dict[str, Any]) -> None:
    """Dispatch a verified Stripe event to its handler. No-ops for unknown types."""
    event_type = event.get("type")
    handler = _HANDLERS.get(event_type)
    if handler is None:
        return
    await handler(session, event.get("data") or {})
