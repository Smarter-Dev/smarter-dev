"""Campaign signups API router.

Handles email/Discord interest capture for marketing campaigns
like the sudo launch waitlist.
"""

from __future__ import annotations

import base64
import logging
import re
from functools import lru_cache
from pathlib import Path
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


_RESOURCES_DIR = Path(__file__).resolve().parents[4] / "resources"


@lru_cache
def _image_b64(name: str) -> str:
    """Load a resource image and return its base64-encoded string."""
    return base64.b64encode((_RESOURCES_DIR / name).read_bytes()).decode()


def _build_confirmation_html(confirm_url: str) -> str:
    """Build the styled HTML for the sudo waitlist confirmation email."""
    bg = _image_b64("email-hex-bg.png")
    logo = _image_b64("email-logo.png")
    return f"""\
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Confirm your email</title>
<!--[if mso]><style>table,td{{font-family:Arial,Helvetica,sans-serif!important}}</style><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#010306;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#020408;background-image:url('data:image/png;base64,{bg}');background-repeat:repeat">
<tr><td align="center" style="padding:32px 16px">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%">
    <tr><td align="center" style="padding:40px 24px 28px"><img src="data:image/png;base64,{logo}" alt="SMARTER Dev" width="306" height="58" style="display:block;border:0;outline:none;width:306px;max-width:100%;height:auto"></td></tr>
    <tr><td align="center" style="padding:0 60px 32px"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="80" style="width:80px"><tr><td style="height:1px;background:linear-gradient(90deg,transparent,#00d4ff,transparent);font-size:1px;line-height:1px">&nbsp;</td></tr></table></td></tr>
    <tr><td style="padding:0 12px"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#080c14;border:1px solid rgba(0,212,255,0.08);border-radius:8px">
      <tr><td style="padding:40px 36px 16px">
        <p style="margin:0 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:700;color:#d4e0ec;line-height:1.3">You've requested elevated privileges.</p>
        <p style="margin:0 0 28px;font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#8098b0;line-height:1.6">You're on the <code style="color:#d4e0ec;font-family:'Courier New',monospace">sudo</code> waitlist. Hit the button below to confirm your email and we'll ping you the moment our premium membership goes live.</p>
      </td></tr>
      <tr><td align="center" style="padding:4px 36px 36px"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td align="center" style="background-color:rgba(0,212,255,0.06);border:1px solid rgba(0,212,255,0.3);border-radius:6px;box-shadow:0 0 20px rgba(0,212,255,0.08),inset 0 0 20px rgba(0,212,255,0.04)"><a href="{confirm_url}" target="_blank" style="display:inline-block;padding:14px 40px;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:600;color:#00d4ff;text-decoration:none;letter-spacing:1px;text-transform:uppercase">Grant Access</a></td></tr></table></td></tr>
      <tr><td style="padding:0 36px"><table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr><td style="height:1px;background-color:rgba(0,212,255,0.06);font-size:1px;line-height:1px">&nbsp;</td></tr></table></td></tr>
      <tr><td style="padding:24px 36px 36px">
        <p style="margin:0 0 12px;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#4a6078;line-height:1.6">If you didn't request this, you can safely ignore this email.</p>
        <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#2a3a4e;line-height:1.5">// link.expires_in = "48h"</p>
      </td></tr>
    </table></td></tr>
    <tr><td align="center" style="padding:32px 24px 16px">
      <p style="margin:0 0 8px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#2a3a4e"><a href="https://discord.gg/de8kajxbYS" target="_blank" style="color:#4a6078;text-decoration:none">Discord</a>&nbsp;&middot;&nbsp;<a href="https://smarter.dev" target="_blank" style="color:#4a6078;text-decoration:none">smarter.dev</a></p>
      <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#1a2836">&copy; 2026 Smarter Dev</p>
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


async def _send_confirmation(email: str, token: str) -> None:
    """Build and send the styled confirmation email."""
    settings = get_settings()
    confirm_url = f"{settings.site_base_url}/api/campaign-signups/confirm?token={token}"
    html = _build_confirmation_html(confirm_url)
    await send_email(email, "sudo: confirm your identity", html)


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
