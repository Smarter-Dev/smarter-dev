"""Polar Checkout creation for the sudo offerings.

Two shapes, but Polar infers both from the product itself:

* **hacker** — a recurring monthly product. Polar manages renewals and dunning;
  cancellation is via the customer portal.
* **founder** — a one-time, pay-what-you-want product (custom price); the buyer
  chooses any amount at or above the $256 minimum on the Polar-hosted page.

The webhook handler is the source of truth for role grants and lifecycle.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User

from smarter_dev.web.billing import catalog
from smarter_dev.web.billing.client import get_polar


class CheckoutError(Exception):
    """Raised when a Checkout cannot be created."""


class UnknownRole(CheckoutError):
    """Raised when an unrecognised offering role is passed to checkout."""


async def create_checkout_session(
    session: AsyncSession,
    user: User,
    *,
    role: str,
    success_url: str,
) -> str:
    """Create a Polar Checkout for the given offering role.

    Role and product all come from the Polar catalog (source of truth). Whether
    it's a subscription or a one-time payment is determined by the product, not
    here. Returns the hosted Checkout URL. Polar's hosted page handles the
    cancel/back path itself, so there is no separate cancel URL.
    """
    offerings = await catalog.get_offerings()
    offering = catalog.get_offering(offerings, role)
    if offering is None:
        raise UnknownRole(f"Unknown sudo offering: {role!r}")

    metadata = {"role": role, "user_id": str(user.id)}
    async with get_polar() as polar:
        checkout = await polar.checkouts.create_async(
            request={
                "products": [offering["product_id"]],
                "metadata": metadata,
                # Link the resulting order/subscription back to this site user;
                # the webhook resolves the membership from checkout metadata.
                "external_customer_id": str(user.id),
                "customer_email": user.email,
                "success_url": success_url,
                "allow_discount_codes": False,
            }
        )
    return checkout.url
