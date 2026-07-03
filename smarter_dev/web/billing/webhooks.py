"""Polar webhook event handling for sudo memberships.

The webhook handler is the source of truth for role grants and membership
lifecycle. Every handler is idempotent and keyed by ``order_id`` /
``subscription_id`` so Polar's automatic retries are safe.

Two offerings, two lifecycles:

* **founder** — ``order.paid`` with ``billing_reason=purchase``. One-time,
  permanent access (``expires_at`` set far in the future). ``order.refunded``
  revokes it. (Polar is Merchant of Record, so it absorbs chargebacks/disputes
  internally — there are no dispute webhooks to handle.)
* **hacker** — ``order.paid`` with ``billing_reason=subscription_create``
  creates the membership with ``expires_at`` = the subscription's current
  period end. Subsequent ``order.paid`` with ``billing_reason=subscription_cycle``
  extends it each renewal. ``subscription.updated`` tracks
  ``cancel_at_period_end``; ``subscription.canceled`` flags a scheduled cancel;
  ``subscription.revoked`` ends access.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.services import assign_role_to_user, remove_role_from_user

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


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a Polar SDK model or a plain dict (test fakes)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


async def _get_by_order_id(
    session: AsyncSession, order_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(SudoMembership.order_id == order_id)
    )
    return result.scalar_one_or_none()


async def _get_by_subscription_id(
    session: AsyncSession, subscription_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.subscription_id == subscription_id
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


def _subscription_period_end(sub: Any) -> datetime:
    """Current period end for a Polar subscription → aware datetime.

    Polar exposes ``current_period_end`` as a datetime (unlike Stripe's epoch).
    Missing period info defaults to "now" so a stale sub doesn't grant forever.
    """
    end = _field(sub, "current_period_end")
    if end is None:
        return datetime.now(tz=timezone.utc)
    if isinstance(end, datetime):
        return end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    # ISO-8601 string fallback (dict test fakes).
    return datetime.fromisoformat(str(end).replace("Z", "+00:00"))


def _order_price_id(order: Any) -> str:
    """Best-effort price id for an order (falls back to the product id)."""
    for item in _field(order, "items", []) or []:
        price_id = _field(item, "product_price_id") or _field(item, "price_id")
        if price_id:
            return str(price_id)
    return str(_field(order, "product_id", "") or "")


def _role_and_user(metadata: Any) -> tuple[str | None, UUID | None]:
    """Extract ``(role, user_id)`` from checkout/subscription metadata."""
    meta = metadata or {}
    role = meta.get("role")
    user_id_raw = meta.get("user_id")
    if role not in ROLE_TO_SKRIFT or not user_id_raw:
        return None, None
    return role, UUID(str(user_id_raw))


async def _converge_quietly(session: AsyncSession, user_id: UUID, where: str) -> None:
    try:
        await converge(session, user_id)
    except Exception:
        logger.exception("converge failed after %s for user %s", where, user_id)


async def handle_order_paid(session: AsyncSession, order: Any) -> None:
    """Fulfil a paid order — founder purchase, hacker signup, or hacker renewal."""
    billing_reason = _field(order, "billing_reason")
    if billing_reason == "purchase":
        await _fulfil_one_time(session, order)
    elif billing_reason in ("subscription_create", "subscription_cycle"):
        await _fulfil_subscription(session, order)
    else:
        logger.info(
            "order.paid with unhandled billing_reason %r; order=%s",
            billing_reason, _field(order, "id"),
        )


async def _fulfil_one_time(session: AsyncSession, order: Any) -> None:
    """Create the permanent founder membership for a one-time order."""
    order_id = _field(order, "id")
    role, user_id = _role_and_user(_field(order, "metadata"))
    if role is None or user_id is None:
        logger.error("order.paid (purchase) missing role/user_id; order=%s", order_id)
        return
    customer_id = _field(order, "customer_id")
    if not customer_id:
        logger.error("order.paid (purchase) missing customer_id; order=%s", order_id)
        return

    # Idempotency: if we've already recorded this order, do nothing.
    if await _get_by_order_id(session, order_id) is not None:
        return

    membership = SudoMembership(
        user_id=user_id,
        role=role,
        source="one_time",
        customer_id=customer_id,
        checkout_id=_field(order, "checkout_id") or order_id,
        order_id=order_id,
        price_id=_order_price_id(order),
        amount_paid_cents=int(_field(order, "total_amount", 0) or 0),
        expires_at=FOUNDER_ACCESS_END,
    )
    session.add(membership)
    await session.commit()

    await _grant_role(session, user_id, role)
    await session.commit()
    await _converge_quietly(session, user_id, "fulfillment")


async def _fulfil_subscription(session: AsyncSession, order: Any) -> None:
    """Create or extend the hacker membership for a subscription order."""
    order_id = _field(order, "id")
    subscription_id = _field(order, "subscription_id")
    if not subscription_id:
        logger.error("subscription order missing subscription_id; order=%s", order_id)
        return

    sub = _field(order, "subscription")
    expires_at = _subscription_period_end(sub)
    will_renew = not bool(_field(sub, "cancel_at_period_end"))

    record = await _get_by_subscription_id(session, subscription_id)
    if record is not None:
        # Renewal (or a re-delivered create): extend the window and re-grant.
        changed = False
        if record.expires_at != expires_at:
            record.expires_at = expires_at
            changed = True
        if record.will_renew != will_renew:
            record.will_renew = will_renew
            changed = True
        if record.revoked_reason is None and changed:
            await session.commit()
            await _grant_role(session, record.user_id, record.role)
            await session.commit()
            await _converge_quietly(session, record.user_id, "renewal")
        return

    # First order for this subscription — create the membership.
    role, user_id = _role_and_user(_field(order, "metadata") or _field(sub, "metadata"))
    if role is None or user_id is None:
        logger.error("subscription order missing role/user_id; order=%s", order_id)
        return
    customer_id = _field(order, "customer_id")
    if not customer_id:
        logger.error("subscription order missing customer_id; order=%s", order_id)
        return

    membership = SudoMembership(
        user_id=user_id,
        role=role,
        source="subscription",
        customer_id=customer_id,
        checkout_id=_field(order, "checkout_id") or order_id,
        subscription_id=subscription_id,
        price_id=_order_price_id(order),
        amount_paid_cents=int(_field(order, "total_amount", 0) or 0),
        will_renew=will_renew,
        expires_at=expires_at,
    )
    session.add(membership)
    await session.commit()

    await _grant_role(session, user_id, role)
    await session.commit()
    await _converge_quietly(session, user_id, "fulfillment")


async def handle_subscription_updated(session: AsyncSession, sub: Any) -> None:
    """Track ``cancel_at_period_end`` and period end for a hacker membership."""
    subscription_id = _field(sub, "id")
    if not subscription_id:
        return
    record = await _get_by_subscription_id(session, subscription_id)
    if record is None:
        return
    record.will_renew = not bool(_field(sub, "cancel_at_period_end"))
    record.expires_at = _subscription_period_end(sub)
    await session.commit()


async def handle_subscription_canceled(session: AsyncSession, sub: Any) -> None:
    """A scheduled cancellation — access continues until the period ends."""
    subscription_id = _field(sub, "id")
    if not subscription_id:
        return
    record = await _get_by_subscription_id(session, subscription_id)
    if record is None:
        return
    record.will_renew = False
    record.expires_at = _subscription_period_end(sub)
    await session.commit()


async def handle_subscription_revoked(session: AsyncSession, sub: Any) -> None:
    """End access when a hacker subscription is fully revoked."""
    subscription_id = _field(sub, "id")
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
    await _converge_quietly(session, record.user_id, "subscription.revoked")


async def handle_order_refunded(session: AsyncSession, order: Any) -> None:
    """Handle ``order.refunded`` for a founder purchase — revoke access."""
    order_id = _field(order, "id")
    if not order_id:
        return
    record = await _get_by_order_id(session, order_id)
    if record is None:
        logger.info("order.refunded for unknown order %s; ignoring.", order_id)
        return
    if record.refunded_at is not None:
        return

    record.refunded_at = datetime.now(tz=timezone.utc)
    record.revoked_reason = "refund"
    await session.commit()

    await _revoke_role(session, record.user_id, record.role)
    await session.commit()
    await _converge_quietly(session, record.user_id, "refund")


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

    Renewal reminders are gone: Hacker is a Polar subscription (Polar owns
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
    "order.paid": handle_order_paid,
    "subscription.updated": handle_subscription_updated,
    "subscription.canceled": handle_subscription_canceled,
    "subscription.revoked": handle_subscription_revoked,
    "order.refunded": handle_order_refunded,
}


async def dispatch(session: AsyncSession, event: Any) -> None:
    """Dispatch a verified Polar event to its handler. No-ops for unknown types.

    ``event`` is a validated Polar webhook payload (``.type`` + ``.data``) or an
    equivalent dict; the handler receives the event's ``data`` object.
    """
    event_type = _field(event, "type")
    handler = _HANDLERS.get(event_type)
    if handler is None:
        return
    await handler(session, _field(event, "data"))
