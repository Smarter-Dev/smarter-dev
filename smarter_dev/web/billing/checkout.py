"""Stripe Checkout Session creation for the sudo founder tiers.

Founder tiers are sold as one-time, one-year purchases — Stripe Checkout in
``payment`` mode, not subscription mode. No auto-renewal. When the year
expires, the user re-purchases at the founder rate (33% off public) within
the grace window, or at public rates outside it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User

from smarter_dev.web.billing import catalog, inventory
from smarter_dev.web.billing.client import get_stripe


class CheckoutError(Exception):
    """Raised when a Checkout Session cannot be created."""


class FounderSeatsExhausted(CheckoutError):
    """Raised when all rwx 0day founder seats have been sold."""


class UnknownTier(CheckoutError):
    """Raised when an unrecognised tier slug is passed to checkout."""


async def create_founder_checkout_session(
    session: AsyncSession,
    user: User,
    *,
    tier: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a one-time Stripe Checkout Session for the given founder tier.

    Tier, price, and role all come from the Stripe catalog (source of truth).
    The webhook handler is the source of truth for inventory; this function
    performs a best-effort check for rwx so the user gets a fast 'sold out'
    response instead of paying and being refunded.
    """
    tiers = await catalog.get_tiers()
    tier_data = catalog.get_tier(tiers, tier)
    if tier_data is None:
        raise UnknownTier(f"Unknown founder tier: {tier!r}")

    price_id = tier_data["price_id"]
    webhook_tier = tier_data["role"]

    if tier == "rwx":
        remaining = await inventory.seats_remaining(session)
        if remaining <= 0:
            raise FounderSeatsExhausted("All rwx 0day founder seats have been claimed.")

    stripe = get_stripe()
    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.id),
        customer_email=user.email,
        allow_promotion_codes=False,
        billing_address_collection="auto",
        # Customer record lets the user manage refunds and link future purchases.
        customer_creation="always",
        payment_intent_data={
            "metadata": {
                "tier": webhook_tier,
                "user_id": str(user.id),
            }
        },
        metadata={
            "tier": webhook_tier,
            "user_id": str(user.id),
        },
    )
    return checkout_session.url
