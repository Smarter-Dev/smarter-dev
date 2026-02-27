"""Campaign signups API router.

Handles email/Discord interest capture for marketing campaigns
like the sudo launch waitlist.
"""

from __future__ import annotations

import logging
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select, and_

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.email import send_email
from smarter_dev.web.models import CampaignSignup

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaign-signups", tags=["Campaign Signups"])


class CampaignSignupRequest(BaseModel):
    campaign_slug: str
    email: str | None = None
    discord_id: str | None = None


async def _send_confirmation(email: str, token: str) -> None:
    """Build and send the confirmation email."""
    settings = get_settings()
    confirm_url = f"{settings.site_base_url}/api/campaign-signups/confirm?token={token}"
    html = (
        "<p>Thanks for signing up for the <strong>sudo</strong> waitlist!</p>"
        f'<p><a href="{confirm_url}">Click here to confirm your email</a></p>'
        "<p>If you didn't request this, you can safely ignore this email.</p>"
    )
    await send_email(email, "Confirm your sudo waitlist signup", html)


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
        existing_signup = existing.scalar_one_or_none()

        if existing_signup:
            if body.email and not existing_signup.email_confirmed:
                # Resend confirmation for unconfirmed email signups
                token = str(uuid4())
                existing_signup.confirmation_token = token
                await session.commit()
                await _send_confirmation(body.email, token)
                return {"status": "ok", "campaign_slug": body.campaign_slug}

            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Already signed up for this campaign.",
            )

        token = str(uuid4()) if body.email else None
        signup = CampaignSignup(
            campaign_slug=body.campaign_slug,
            email=body.email,
            discord_id=body.discord_id,
            email_confirmed=False,
            confirmation_token=token,
        )
        session.add(signup)
        await session.commit()

        if body.email and token:
            await _send_confirmation(body.email, token)

        logger.info(
            "Campaign signup created: %s for %s",
            body.email or body.discord_id,
            body.campaign_slug,
        )

    return {"status": "ok", "campaign_slug": body.campaign_slug}


_CONFIRM_SUCCESS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Email Confirmed</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0a0a0a;color:#e0e0e0}
.card{text-align:center;padding:2rem;border:1px solid #333;border-radius:12px;max-width:400px}
h1{margin:0 0 .5rem;font-size:1.5rem}p{color:#999}</style></head>
<body><div class="card"><h1>Email confirmed!</h1><p>You're on the sudo waitlist. We'll be in touch.</p></div></body>
</html>"""

_CONFIRM_ERROR_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Invalid Link</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0a0a0a;color:#e0e0e0}
.card{text-align:center;padding:2rem;border:1px solid #333;border-radius:12px;max-width:400px}
h1{margin:0 0 .5rem;font-size:1.5rem}p{color:#999}</style></head>
<body><div class="card"><h1>Invalid or expired link</h1><p>This confirmation link is no longer valid. Please sign up again.</p></div></body>
</html>"""


@router.get("/confirm")
async def confirm_email(token: str) -> HTMLResponse:
    """Confirm a campaign signup email address."""
    async with get_db_session_context() as session:
        result = await session.execute(
            select(CampaignSignup).where(
                CampaignSignup.confirmation_token == token
            )
        )
        signup = result.scalar_one_or_none()

        if not signup:
            return HTMLResponse(content=_CONFIRM_ERROR_HTML, status_code=400)

        signup.email_confirmed = True
        signup.confirmation_token = None
        await session.commit()

        logger.info("Email confirmed for signup %s", signup.id)

    return HTMLResponse(content=_CONFIRM_SUCCESS_HTML)
