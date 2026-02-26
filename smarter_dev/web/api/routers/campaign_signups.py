"""Campaign signups API router.

Handles email/Discord interest capture for marketing campaigns
like the sudo launch waitlist.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, and_

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import CampaignSignup

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaign-signups", tags=["Campaign Signups"])


class CampaignSignupRequest(BaseModel):
    campaign_slug: str
    email: str | None = None
    discord_id: str | None = None


@router.post("", status_code=201)
async def create_signup(body: CampaignSignupRequest) -> dict:
    """Register interest in a campaign."""
    if not body.email and not body.discord_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Either email or discord_id is required.",
        )

    # Validate email format
    if body.email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", body.email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid email format.",
        )

    # Validate discord_id format (numeric snowflake)
    if body.discord_id and not re.match(r"^\d{17,20}$", body.discord_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Discord ID format.",
        )

    async with get_db_session_context() as session:
        # Check for duplicate
        conditions = [CampaignSignup.campaign_slug == body.campaign_slug]
        if body.email:
            conditions.append(CampaignSignup.email == body.email)
        elif body.discord_id:
            conditions.append(CampaignSignup.discord_id == body.discord_id)

        existing = await session.execute(
            select(CampaignSignup).where(and_(*conditions))
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Already signed up for this campaign.",
            )

        signup = CampaignSignup(
            campaign_slug=body.campaign_slug,
            email=body.email,
            discord_id=body.discord_id,
        )
        session.add(signup)
        await session.commit()

        logger.info(
            "Campaign signup created: %s for %s",
            body.email or body.discord_id,
            body.campaign_slug,
        )

    return {"status": "ok", "campaign_slug": body.campaign_slug}
