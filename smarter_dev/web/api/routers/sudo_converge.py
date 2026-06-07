"""Internal sudo converge endpoint.

Called by the Discord bot on ``GUILD_MEMBER_ADD`` (and any other event
that should re-project sudo roles) with a ``discord_user_id``. The
endpoint looks up the linked site user via Skrift's ``oauth_accounts``
table and runs ``converge`` against the current entitlement state.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.web.api.dependencies import APIKey
from smarter_dev.web.billing.converge import converge

from fastapi import Depends

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sudo", tags=["Sudo Converge"])

SkriftSession = Annotated[AsyncSession, Depends(get_skrift_db_session)]


class ConvergeRequest(BaseModel):
    discord_user_id: str = Field(..., description="Discord user ID to converge")


class ConvergeResponse(BaseModel):
    user_id: str | None = Field(None, description="Linked site user id, if any")
    added: list[str] = Field(default_factory=list, description="Role IDs added")
    removed: list[str] = Field(default_factory=list, description="Role IDs removed")
    linked: bool = Field(..., description="True if a site user was found")


@router.post("/converge", response_model=ConvergeResponse)
async def converge_by_discord(
    body: ConvergeRequest,
    db_session: SkriftSession,
    api_key: APIKey,
) -> ConvergeResponse:
    """Trigger converge for whichever site user holds this Discord ID.

    No-op (still returns 200) if no site account is linked yet — the next
    trigger (link, member-add, daily sweep) heals.
    """
    result = await db_session.execute(
        text(
            "SELECT user_id FROM skrift.oauth_accounts "
            "WHERE provider = 'discord' AND provider_account_id = :did "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"did": body.discord_user_id},
    )
    row = result.first()
    if row is None:
        return ConvergeResponse(user_id=None, linked=False)

    user_id = UUID(str(row[0]))
    try:
        outcome = await converge(db_session, user_id)
    except Exception:
        logger.exception("converge endpoint: unexpected failure for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="converge failed",
        )
    return ConvergeResponse(
        user_id=str(user_id),
        linked=True,
        added=outcome["added"],
        removed=outcome["removed"],
    )
