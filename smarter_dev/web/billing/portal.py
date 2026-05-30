"""Stripe Customer Portal session creation."""

from __future__ import annotations

from smarter_dev.shared.config import get_settings
from smarter_dev.web.billing.client import get_stripe


def create_portal_session(stripe_customer_id: str, *, return_url: str | None = None) -> str:
    """Create a Stripe Customer Portal session and return its URL."""
    settings = get_settings()
    stripe = get_stripe()
    portal = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url or f"{settings.site_base_url.rstrip('/')}/account/billing",
    )
    return portal.url
