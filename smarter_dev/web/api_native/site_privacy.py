"""Site service API for a Discord member's AI assistant opt-out."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from litestar import Controller
from litestar import Response
from litestar import delete
from litestar import get
from litestar import post
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from skrift.auth.guards import Permission
from skrift.db.models.user import User
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.privacy_purge import SNOWFLAKE_PATTERN
from smarter_dev.shared.privacy_purge import Snowflake
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.chat.dispatch import create_dispatch
from smarter_dev.web.chat.dispatch import dispatch_one
from smarter_dev.web.chat_bot_opt_out import opt_in
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_bot_opt_out import read_opt_out
from smarter_dev.web.chat_bot_purge import STATUS_CLOSED
from smarter_dev.web.models import AccountDeletionRequest
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotPurgeRequest

SITE_PRIVACY_GUARDS = [*SITE_API_GUARDS, Permission("site-privacy")]
_DISCORD_USER_ID = re.compile(SNOWFLAKE_PATTERN)


def _validate_discord_user_id(discord_user_id: str) -> None:
    if _DISCORD_USER_ID.fullmatch(discord_user_id) is None:
        raise plain_error(400, "Invalid Discord user ID")


class SiteOptOutState(BaseModel):
    opted_out: bool
    since: datetime | None
    source: Literal["opt_out", "purge"] | None


async def _state(session: AsyncSession, discord_user_id: str) -> SiteOptOutState:
    state = await read_opt_out(session, discord_user_id)
    since = None
    if state.opted_out:
        since = await session.scalar(
            select(ChatBotBlockedUser.created_at).where(
                ChatBotBlockedUser.discord_user_id == discord_user_id
            )
        )
    return SiteOptOutState(opted_out=state.opted_out, since=since, source=state.source)


async def _refuse_in_flight_purge(session: AsyncSession, discord_user_id: str) -> None:
    request = await session.scalar(
        select(ChatBotPurgeRequest.id)
        .where(
            ChatBotPurgeRequest.discord_user_id == discord_user_id,
            ChatBotPurgeRequest.status != STATUS_CLOSED,
        )
        .limit(1)
    )
    if request is not None:
        raise plain_error(409, "A privacy purge is in progress for this member")


class SitePrivacyController(Controller):
    path = "/api/site/privacy"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post(
        "/delete-account/{user_id:str}",
        status_code=HTTP_200_OK,
        guards=SITE_PRIVACY_GUARDS,
    )
    async def delete_account(
        self, db_session: AsyncSession, user_id: str
    ) -> Response[dict[str, str]]:
        """Queue the same durable purge used by the Python account page.

        The Worker polls until the user disappears. The purge worker anonymizes
        its successful receipt, so a 404 after a 202 means the purge finished.
        """
        try:
            account_id = UUID(user_id)
        except ValueError:
            raise plain_error(400, "Invalid account ID") from None
        if str(account_id) != user_id.lower():
            raise plain_error(400, "Invalid account ID")

        # Match the account page's row lock: attachment uploads also lock the
        # user before committing an object-store write.
        existing_id = await db_session.scalar(
            select(User.id).where(User.id == account_id).with_for_update()
        )
        deletion = await db_session.scalar(
            select(AccountDeletionRequest).where(
                AccountDeletionRequest.user_id == account_id
            )
        )
        if existing_id is None:
            raise plain_error(404, "Account not found")
        await db_session.execute(
            update(User).where(User.id == account_id).values(is_active=False)
        )
        if deletion is None:
            deletion = AccountDeletionRequest(user_id=account_id, status="pending")
            db_session.add(deletion)
            await db_session.flush()
        dispatch = await create_dispatch(
            db_session,
            job_type="chat.account.delete",
            aggregate_id=deletion.id,
            payload={"request_id": str(deletion.id)},
        )
        await db_session.commit()
        try:
            await dispatch_one(dispatch.id)
        except Exception:
            # The transactional outbox retries submission.
            pass
        return Response({"status": "pending"}, status_code=202)

    @get(
        "/opt-out/{discord_user_id:str}",
        status_code=HTTP_200_OK,
        guards=SITE_PRIVACY_GUARDS,
    )
    async def read(
        self, db_session: AsyncSession, discord_user_id: Snowflake
    ) -> SiteOptOutState:
        _validate_discord_user_id(discord_user_id)
        return await _state(db_session, discord_user_id)

    @post(
        "/opt-out/{discord_user_id:str}",
        status_code=HTTP_200_OK,
        guards=SITE_PRIVACY_GUARDS,
    )
    async def enable(
        self, db_session: AsyncSession, discord_user_id: Snowflake
    ) -> SiteOptOutState:
        _validate_discord_user_id(discord_user_id)
        await _refuse_in_flight_purge(db_session, discord_user_id)
        await opt_out(db_session, discord_user_id)
        result = await _state(db_session, discord_user_id)
        await db_session.commit()
        return result

    @delete(
        "/opt-out/{discord_user_id:str}",
        status_code=HTTP_200_OK,
        guards=SITE_PRIVACY_GUARDS,
    )
    async def disable(
        self, db_session: AsyncSession, discord_user_id: Snowflake
    ) -> SiteOptOutState:
        _validate_discord_user_id(discord_user_id)
        await _refuse_in_flight_purge(db_session, discord_user_id)
        await opt_in(db_session, discord_user_id)
        result = await _state(db_session, discord_user_id)
        await db_session.commit()
        return result
