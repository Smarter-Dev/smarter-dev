"""The campaign and waitlist sign-ups that belong to a site account.

``campaign_signups`` has no link to an account: a row holds an email address,
a Discord ID or both. A row belongs to an account when one of its contacts is
one the account has proved it controls:

- an email address a linked login (``oauth_accounts``) holds with
  ``provider_email_verified`` set: the provider attested it, or the member
  answered Skrift's email challenge before the login was linked. The account's
  own ``users.email`` comes from a login, and Skrift can store it unverified
  when the account is created, so it counts only through such a login;
- the Discord user ID of a linked Discord login, which signing in with it
  proves.

Email addresses match without regard to case, as mail systems deliver them.
The member sees and deletes only rows found this way, and deleting the account
deletes them too (``delete_account_leftovers``).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from skrift.db.models.oauth_account import OAuthAccount
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import CampaignSignup

# What a member sees for each campaign; an unknown slug is shown as words.
CAMPAIGN_NAMES = {"sudo-launch": "sudo membership waitlist"}


@dataclass(frozen=True)
class AccountContacts:
    emails: frozenset[str]
    discord_ids: frozenset[str]


@dataclass(frozen=True)
class AccountSignup:
    id: UUID
    campaign: str
    email: str | None
    discord: bool
    email_confirmed: bool


async def account_contacts(session: AsyncSession, user_id: UUID) -> AccountContacts:
    """The email addresses and Discord IDs the account has proved it controls."""
    rows = (
        await session.execute(
            select(
                OAuthAccount.provider,
                OAuthAccount.provider_account_id,
                OAuthAccount.provider_email,
                OAuthAccount.provider_email_verified,
            ).where(OAuthAccount.user_id == user_id)
        )
    ).all()
    return AccountContacts(
        emails=frozenset(
            email.strip().lower()
            for _provider, _subject, email, verified in rows
            if verified and email and email.strip()
        ),
        discord_ids=frozenset(
            subject
            for provider, subject, _email, _verified in rows
            if provider == "discord"
        ),
    )


def _belongs_to(contacts: AccountContacts):
    conditions = []
    if contacts.emails:
        conditions.append(func.lower(CampaignSignup.email).in_(sorted(contacts.emails)))
    if contacts.discord_ids:
        conditions.append(CampaignSignup.discord_id.in_(sorted(contacts.discord_ids)))
    return or_(*conditions)


def campaign_name(slug: str) -> str:
    return CAMPAIGN_NAMES.get(slug) or slug.replace("-", " ").replace("_", " ")


async def list_account_signups(
    session: AsyncSession, user_id: UUID
) -> list[AccountSignup]:
    """The account's sign-ups, oldest first, showing only its own contacts."""
    contacts = await account_contacts(session, user_id)
    if not (contacts.emails or contacts.discord_ids):
        return []
    signups = (
        await session.scalars(
            select(CampaignSignup)
            .where(_belongs_to(contacts))
            .order_by(CampaignSignup.created_at, CampaignSignup.id)
        )
    ).all()
    result = []
    for signup in signups:
        # A row found by one contact may hold another; show only the member's.
        own_email = (
            signup.email
            if signup.email and signup.email.strip().lower() in contacts.emails
            else None
        )
        result.append(
            AccountSignup(
                id=signup.id,
                campaign=campaign_name(signup.campaign_slug),
                email=own_email,
                discord=signup.discord_id in contacts.discord_ids,
                email_confirmed=bool(own_email and signup.email_confirmed),
            )
        )
    return result


async def delete_account_signups(
    session: AsyncSession, user_id: UUID, signup_id: UUID | None = None
) -> int:
    """Delete the account's sign-ups, or just ``signup_id`` if it is one of them.

    Returns how many rows went. The caller commits.
    """
    contacts = await account_contacts(session, user_id)
    if not (contacts.emails or contacts.discord_ids):
        return 0
    statement = delete(CampaignSignup).where(_belongs_to(contacts))
    if signup_id is not None:
        statement = statement.where(CampaignSignup.id == signup_id)
    result = await session.execute(
        statement.execution_options(synchronize_session=False)
    )
    return result.rowcount or 0
