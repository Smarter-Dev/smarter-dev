"""Polar customer portal session creation."""

from __future__ import annotations

from smarter_dev.shared.config import get_settings
from smarter_dev.web.billing.client import get_polar


async def create_portal_session(
    customer_id: str, *, return_url: str | None = None
) -> str:
    """Create a Polar customer session and return its customer portal URL."""
    settings = get_settings()
    async with get_polar() as polar:
        customer_session = await polar.customer_sessions.create_async(
            request={
                "customer_id": customer_id,
                "return_url": return_url
                or f"{settings.site_base_url.rstrip('/')}/account/billing",
            }
        )
    return customer_session.customer_portal_url
