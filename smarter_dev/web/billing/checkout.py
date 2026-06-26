"""Stripe Checkout Session creation for the sudo offerings.

Two shapes:

* **hacker** — ``mode=subscription``. A recurring $8/mo price. Stripe manages
  renewals and dunning; cancellation is via the customer portal.
* **founder** — ``mode=payment``. A one-time, pay-what-you-want price
  (``custom_unit_amount``); the buyer chooses any amount at or above the $256
  minimum at the Stripe-hosted page.

The webhook handler is the source of truth for role grants and lifecycle.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User

from smarter_dev.web.billing import catalog
from smarter_dev.web.billing.client import get_stripe


class CheckoutError(Exception):
    """Raised when a Checkout Session cannot be created."""


class UnknownRole(CheckoutError):
    """Raised when an unrecognised offering role is passed to checkout."""


async def create_checkout_session(
    session: AsyncSession,
    user: User,
    *,
    role: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a Stripe Checkout Session for the given offering role.

    Role, price, and mode all come from the Stripe catalog (source of truth).
    Returns the hosted Checkout URL.
    """
    offerings = await catalog.get_offerings()
    offering = catalog.get_offering(offerings, role)
    if offering is None:
        raise UnknownRole(f"Unknown sudo offering: {role!r}")

    price_id = offering["price_id"]
    mode = "subscription" if offering["recurring"] else "payment"
    metadata = {"role": role, "user_id": str(user.id)}

    params: dict = {
        "mode": mode,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(user.id),
        "customer_email": user.email,
        "allow_promotion_codes": False,
        "metadata": metadata,
    }
    if mode == "subscription":
        # Subscription mode always creates a Customer; stamp the metadata onto
        # the subscription so the webhook can resolve it.
        params["subscription_data"] = {"metadata": metadata}
    else:
        # One-time payment: create a Customer so the buyer can manage refunds
        # and we can link future purchases to the same identity.
        params["customer_creation"] = "always"
        params["billing_address_collection"] = "auto"
        params["payment_intent_data"] = {"metadata": metadata}

    stripe = get_stripe()
    checkout_session = stripe.checkout.Session.create(**params)
    return checkout_session.url
