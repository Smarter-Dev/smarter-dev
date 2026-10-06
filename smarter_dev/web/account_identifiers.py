"""Who-did-it columns that name a site account deleting itself.

Admin pages and the bot record who set something up as a free-text value with
no foreign key, so deleting the user row leaves it behind. The account
deletion job (``delete_account_leftovers``) rewrites each value that is
exactly one of the account's identifiers, ignoring case, to ``DELETED``:

- the account's name, its email and every email a linked login confirmed
  (``extension_installs.installed_by`` records the admin's email, or the name
  when there is none);
- the Discord ID of its linked Discord login (the bot records an admin
  handler's ``created_by_admin`` as the admin's Discord ID).

Markers the app writes itself (``admin``, ``extension``, ``chatbot``,
``deleted``) are never rewritten, whatever the account is called. The purge
requests the account opened lose their ``requested_by``, which holds its user
ID.
"""

from __future__ import annotations

from uuid import UUID

from skrift.db.models.user import User
from sqlalchemy import func
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.account_signups import account_contacts
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import Campaign
from smarter_dev.web.models import ChannelHandler
from smarter_dev.web.models import ChatBotPurgeRequest
from smarter_dev.web.models import ExtensionInstall
from smarter_dev.web.models import ForumAgent
from smarter_dev.web.models import RepeatingMessage
from smarter_dev.web.models import ScheduledMessage
from smarter_dev.web.models import SquadSaleEvent

DELETED = "DELETED"
APP_MARKERS = frozenset({"admin", "extension", "chatbot", "deleted"})

CREATOR_COLUMNS = (
    ExtensionInstall.installed_by,
    ChannelHandler.created_by,
    ForumAgent.created_by,
    Campaign.created_by,
    ScheduledMessage.created_by,
    SquadSaleEvent.created_by,
    RepeatingMessage.created_by,
    AdminHandler.created_by_admin,
)


async def account_identifiers(session: AsyncSession, user_id: UUID) -> list[str]:
    """The account's identifiers as stored creator values could hold them, lowercased."""
    user = await session.get(User, user_id)
    contacts = await account_contacts(session, user_id)
    values = {*contacts.emails, *contacts.discord_ids}
    if user is not None:
        values.update(
            value.strip().lower() for value in (user.name, user.email) if value
        )
    return sorted(value for value in values if value and value not in APP_MARKERS)


async def forget_admin_identifiers(session: AsyncSession, user_id: UUID) -> None:
    """Rewrite creator values naming the account; the caller commits."""
    identifiers = await account_identifiers(session, user_id)
    if identifiers:
        for column in CREATOR_COLUMNS:
            await session.execute(
                update(column.class_)
                .where(func.lower(column).in_(identifiers))
                .values({column.key: DELETED})
                .execution_options(synchronize_session=False)
            )
    await session.execute(
        update(ChatBotPurgeRequest)
        .where(ChatBotPurgeRequest.requested_by == str(user_id))
        .values(requested_by=None)
        .execution_options(synchronize_session=False)
    )
