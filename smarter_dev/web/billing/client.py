"""Stripe SDK client initialised from project settings."""

from __future__ import annotations

import stripe

from smarter_dev.shared.config import get_settings


def get_stripe() -> "stripe":
    """Return the Stripe SDK module with the secret key configured.

    Stripe's Python SDK is module-level (no client object), so this just
    ensures ``stripe.api_key`` is set before callers reach into it. Raises if
    the secret key isn't configured — billing endpoints should refuse to run
    rather than silently call against a misconfigured client.
    """
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not configured; cannot reach the Stripe API."
        )
    stripe.api_key = settings.stripe_secret_key
    return stripe
