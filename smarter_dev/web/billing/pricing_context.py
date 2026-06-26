"""Shared render context for the sudo offerings.

Both the /sudo pricing page and the homepage teaser show the same two live
offerings (Hacker, Founder), sourced from Stripe, so the two surfaces never
drift.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.billing import catalog


async def build_pricing_context(session: AsyncSession) -> dict[str, Any]:
    """Return the two offerings (from Stripe), plus a by-role lookup.

    ``session`` is accepted for signature parity with the controllers (and in
    case a future surface needs the caller's membership state); the offerings
    themselves come from the Stripe-backed catalog cache.
    """
    offerings = await catalog.get_offerings()
    by_role = {o["role"]: o for o in offerings}
    return {
        "offerings": offerings,
        "offerings_by_role": by_role,
        "hacker": by_role.get("hacker"),
        "founder": by_role.get("founder"),
    }
