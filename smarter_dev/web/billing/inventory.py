"""Founder seat inventory accounting.

Source of truth lives in ``sudo_memberships.founder_seat_number``: any row
with a non-NULL value occupies a seat. Seats stay burned even after a refund
so the rwx 0day cap stays honored against historical purchases.
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.web.models import SudoMembership


async def seats_taken(session: AsyncSession) -> int:
    """How many founder seats have ever been assigned."""
    result = await session.execute(
        select(func.count(SudoMembership.id)).where(
            SudoMembership.founder_seat_number.is_not(None)
        )
    )
    return int(result.scalar() or 0)


async def seats_remaining(session: AsyncSession) -> int:
    """Founder seats still available for purchase."""
    settings = get_settings()
    taken = await seats_taken(session)
    return max(0, settings.sudo_founder_seat_limit - taken)


async def reserve_next_seat(session: AsyncSession) -> int | None:
    """Lock the seat-count rows and return the next seat number to assign.

    Uses ``SELECT … FOR UPDATE`` against the existing founder rows so that
    two concurrent webhook handlers can't both pick the same seat number.
    Returns ``None`` when the cap has been reached.

    The caller is responsible for inserting the ``sudo_memberships`` row
    with the returned seat number inside the same transaction.
    """
    settings = get_settings()

    # Lock all currently-occupied founder rows so concurrent writers wait.
    await session.execute(
        text(
            "SELECT id FROM sudo_memberships "
            "WHERE founder_seat_number IS NOT NULL FOR UPDATE"
        )
    )

    taken = await seats_taken(session)
    if taken >= settings.sudo_founder_seat_limit:
        return None
    return taken + 1
