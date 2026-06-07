"""Renewal-reminder emails for sudo one-time founder memberships.

Spec calls for reminder emails at 30 / 7 / 1 days before ``expires_at``
on one-time founder purchases. Skipped if the membership is already
revoked (refund / dispute / admin clamp). Each (membership, threshold)
pair is sent at most once thanks to the
``sudo_membership_reminders`` table's unique constraint — the daily
sweep blindly try-inserts and an IntegrityError short-circuits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User

from smarter_dev.shared.email import send_email
from smarter_dev.web.models import SudoMembership, SudoMembershipReminder

logger = logging.getLogger(__name__)

REMINDER_THRESHOLDS = (30, 7, 1)


def _render_subject(days_before: int, tier_label: str) -> str:
    if days_before == 1:
        return f"Your sudo {tier_label} expires tomorrow"
    return f"Your sudo {tier_label} expires in {days_before} days"


def _render_html(membership: SudoMembership, days_before: int) -> str:
    tier_label = {"read": "r--", "write": "rw-", "execute": "rwx"}.get(
        membership.tier, membership.tier,
    )
    expires_str = membership.expires_at.strftime("%-d %B %Y")
    when = "tomorrow" if days_before == 1 else f"in {days_before} days"
    return f"""\
<!DOCTYPE html>
<html>
  <body style="font-family: system-ui, sans-serif; max-width: 540px; margin: 0 auto; padding: 24px; color: #1a1a1a;">
    <h1 style="font-size: 1.4rem; margin: 0 0 16px;">sudo {tier_label} expires {when}</h1>
    <p>Your founder year of sudo {tier_label} access ends on <strong>{expires_str}</strong>.</p>
    <p>
      Renew at the founder rate (33% off the public price, locked in) inside the 30-day
      window and your founder pricing stays with you. Wait longer than 30 days after
      expiry and you re-enter at the public rate.
    </p>
    <p style="margin: 28px 0;">
      <a
        href="https://smarter.dev/sudo"
        style="background: #00d4ff; color: #04141d; padding: 12px 20px; text-decoration: none; font-weight: 600; font-family: monospace; font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase;"
      >Renew at the founder rate →</a>
    </p>
    <p style="color: #6b7280; font-size: 13px;">
      Manage billing and download invoices in the
      <a href="https://smarter.dev/account/billing">Stripe portal on your account</a>.
    </p>
  </body>
</html>
"""


async def _record_sent(
    session: AsyncSession, membership_id: UUID, days_before: int,
) -> bool:
    """Try to insert the reminder row. Return True if we won the race, False
    if the row already existed (duplicate threshold)."""
    row = SudoMembershipReminder(
        membership_id=membership_id,
        days_before=days_before,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def _user_email(session: AsyncSession, user_id: UUID) -> str | None:
    result = await session.execute(select(User.email).where(User.id == user_id))
    row = result.first()
    return row[0] if row and row[0] else None


async def _send_one(
    session: AsyncSession, membership: SudoMembership, days_before: int,
) -> bool:
    """Attempt to send a single reminder. Returns True if a mail was sent
    (or recorded for non-emailable users), False otherwise."""
    # Reserve the row first so we never double-send across concurrent sweeps.
    if not await _record_sent(session, membership.id, days_before):
        return False
    await session.commit()

    email = await _user_email(session, membership.user_id)
    if not email:
        logger.info(
            "reminder: user %s has no email; marker recorded but no mail sent.",
            membership.user_id,
        )
        return False

    subject = _render_subject(days_before, membership.tier)
    html = _render_html(membership, days_before)
    try:
        await send_email(email, subject, html)
        logger.info(
            "reminder sent: user=%s threshold=%dd tier=%s",
            membership.user_id, days_before, membership.tier,
        )
        return True
    except Exception:
        logger.exception(
            "reminder: send_email raised for user %s threshold %dd",
            membership.user_id, days_before,
        )
        # The DB row stays — better to drop one notification than to email
        # the user three times because the mailer flapped.
        return False


def _eligible_query(now: datetime, days_before: int):
    """Memberships eligible for the ``days_before`` reminder right now."""
    upper = now + timedelta(days=days_before)
    return (
        select(SudoMembership)
        .where(SudoMembership.source == "one_time")
        .where(SudoMembership.revoked_reason.is_(None))
        .where(SudoMembership.expires_at > now)
        .where(SudoMembership.expires_at <= upper)
    )


async def send_renewal_reminders(session: AsyncSession) -> dict[str, int]:
    """Send any due renewal reminders. Returns counts by threshold.

    Order matters: 30-day first, then 7, then 1. The unique constraint
    means a membership that crosses two thresholds between sweeps only
    sends the smaller one for whichever threshold hasn't been sent yet.
    """
    now = datetime.now(tz=timezone.utc)
    sent_by_threshold: dict[str, int] = {}
    for days_before in REMINDER_THRESHOLDS:
        result = await session.execute(_eligible_query(now, days_before))
        memberships = list(result.scalars())
        sent = 0
        for membership in memberships:
            ok = await _send_one(session, membership, days_before)
            if ok:
                sent += 1
        sent_by_threshold[f"{days_before}d"] = sent
    logger.info("sudo renewal reminders: %s", sent_by_threshold)
    return sent_by_threshold
