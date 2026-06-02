"""Shared render context for the founder pricing ladder.

Both the /sudo pricing page and the homepage ground-floor teaser show the same
live tiers, seat cap, and remaining-seat dots. This builds that context once so
the two surfaces never drift.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.web.billing import catalog, inventory


async def build_pricing_context(session: AsyncSession) -> dict[str, Any]:
    """Return tiers (from Stripe), seat totals, and per-seat dots."""
    settings = get_settings()
    seats_total = settings.sudo_founder_seat_limit
    seats_left = await inventory.seats_remaining(session)
    seats_claimed = max(0, seats_total - seats_left)

    # Tiers come from Stripe (source of truth); the rwx tag carries a
    # {seats} placeholder filled from the configured seat cap.
    tiers = await catalog.get_tiers()
    for tier in tiers:
        if "{seats}" in tier["tag"]:
            tier["tag"] = tier["tag"].format(seats=seats_total)

    seat_dots = [{"claimed": i < seats_claimed} for i in range(seats_total)]

    return {
        "tiers": tiers,
        "seats_total": seats_total,
        "seats_left": seats_left,
        "seats_claimed": seats_claimed,
        "seat_dots": seat_dots,
    }
