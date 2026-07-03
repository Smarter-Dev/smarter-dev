"""Polar SDK client initialised from project settings."""

from __future__ import annotations

from polar_sdk import Polar

from smarter_dev.shared.config import get_settings


def get_polar() -> Polar:
    """Return a configured Polar client.

    Unlike Stripe's module-level SDK, Polar's client is an object with its own
    HTTP transport, so callers should use it as a context manager::

        async with get_polar() as polar:
            await polar.checkouts.create_async(request=...)

    Raises if the access token isn't configured — billing endpoints should
    refuse to run rather than silently call against a misconfigured client.
    """
    settings = get_settings()
    if not settings.polar_access_token:
        raise RuntimeError(
            "POLAR_ACCESS_TOKEN is not configured; cannot reach the Polar API."
        )
    return Polar(
        access_token=settings.polar_access_token,
        server=settings.polar_server,
    )
