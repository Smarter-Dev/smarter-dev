"""Stripe Checkout Session creation for the sudo founder tiers.

Founder tiers are sold as one-time, one-year purchases — Stripe Checkout in
``payment`` mode, not subscription mode. No auto-renewal. When the year
expires, the user re-purchases at the founder rate (33% off public) within
the grace window, or at public rates outside it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User

from smarter_dev.shared.config import get_settings
from smarter_dev.web.billing import inventory
from smarter_dev.web.billing.client import get_stripe


class CheckoutError(Exception):
    """Raised when a Checkout Session cannot be created."""


class FounderSeatsExhausted(CheckoutError):
    """Raised when all rwx 0day founder seats have been sold."""


class UnknownTier(CheckoutError):
    """Raised when an unrecognised tier slug is passed to checkout."""


# Tier slug → (settings attribute holding the Stripe price ID, webhook tier label)
_TIER_CONFIG: dict[str, tuple[str, str]] = {
    "r":   ("stripe_r_annual_price_id",   "read"),
    "rw":  ("stripe_rw_annual_price_id",  "write"),
    "rwx": ("stripe_rwx_annual_price_id", "execute"),
}


async def create_founder_checkout_session(
    session: AsyncSession,
    user: User,
    *,
    tier: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a one-time Stripe Checkout Session for the given founder tier.

    The webhook handler is the source of truth for inventory; this function
    performs a best-effort check for rwx so the user gets a fast 'sold out'
    response instead of paying and being refunded.
    """
    if tier not in _TIER_CONFIG:
        raise UnknownTier(f"Unknown founder tier: {tier!r}")

    settings = get_settings()
    settings_attr, webhook_tier = _TIER_CONFIG[tier]
    price_id = getattr(settings, settings_attr)
    if not price_id:
        raise CheckoutError(
            f"{settings_attr.upper()} is not configured; cannot create checkout."
        )

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
