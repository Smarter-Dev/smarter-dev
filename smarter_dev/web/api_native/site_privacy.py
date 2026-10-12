"""Site service API for a Discord member's AI assistant opt-out."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from litestar import Controller
from litestar import delete
from litestar import get
from litestar import post
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from skrift.auth.guards import Permission
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.privacy_purge import Snowflake
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.chat_bot_opt_out import opt_in
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_bot_opt_out import read_opt_out
from smarter_dev.web.chat_bot_purge import STATUS_CLOSED
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotPurgeRequest

SITE_PRIVACY_GUARDS = [*SITE_API_GUARDS, Permission("site-privacy")]


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

    @get(
        "/opt-out/{discord_user_id:str}",
        status_code=HTTP_200_OK,
        guards=SITE_PRIVACY_GUARDS,
    )
    async def read(
        self, db_session: AsyncSession, discord_user_id: Snowflake
    ) -> SiteOptOutState:
        return await _state(db_session, discord_user_id)

    @post(
        "/opt-out/{discord_user_id:str}",
        status_code=HTTP_200_OK,
        guards=SITE_PRIVACY_GUARDS,
    )
    async def enable(
        self, db_session: AsyncSession, discord_user_id: Snowflake
    ) -> SiteOptOutState:
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
        await _refuse_in_flight_purge(db_session, discord_user_id)
        await opt_in(db_session, discord_user_id)
        result = await _state(db_session, discord_user_id)
        await db_session.commit()
        return result
