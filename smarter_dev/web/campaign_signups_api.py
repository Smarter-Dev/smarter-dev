"""Campaign signups API — Litestar controller.

Handles email/Discord interest capture for marketing campaigns
like the sudo launch waitlist. Lives at /v2/api/campaign-signups
and uses Skrift's DB session (primary database).
"""

from __future__ import annotations

import base64
import logging
import re
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from litestar import Controller, post
from litestar.exceptions import HTTPException
from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_ENTITY,
)
from msgspec import Struct
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.email import send_email
from smarter_dev.web.models import CampaignSignup

logger = logging.getLogger(__name__)

_RESOURCES_DIR = Path(__file__).resolve().parents[2] / "resources"


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
    confirm_url = f"{settings.site_base_url}/sudo/confirm?token={token}"
    html = _build_confirmation_html(confirm_url)
    await send_email(email, "sudo: confirm your identity", html)


class SignupBody(Struct):
    """Request body for campaign signup."""

    campaign_slug: str
    email: str | None = None
    discord_id: str | None = None


class CampaignSignupsApiController(Controller):
    """Campaign signups REST API using Skrift's primary database."""

    path = "/v2/api/campaign-signups"

    @post("", status_code=HTTP_201_CREATED)
    async def create_signup(
        self, data: SignupBody, db_session: AsyncSession
    ) -> dict:
        """Register interest in a campaign."""
        if not data.email and not data.discord_id:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Either email or discord_id is required.",
            )

        if data.email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", data.email):
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid email format.",
            )

        if data.discord_id and not re.match(r"^\d{17,20}$", data.discord_id):
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid Discord ID format.",
            )

        # Check for duplicate
        conditions = [CampaignSignup.campaign_slug == data.campaign_slug]
        if data.email:
            conditions.append(CampaignSignup.email == data.email)
        elif data.discord_id:
            conditions.append(CampaignSignup.discord_id == data.discord_id)

        existing = await db_session.execute(
            select(CampaignSignup).where(and_(*conditions))
        )
        existing_signup = existing.scalar_one_or_none()

        if existing_signup:
            if data.email and not existing_signup.email_confirmed:
                # Resend confirmation for unconfirmed email signups
                token = str(uuid4())
                existing_signup.confirmation_token = token
                await db_session.commit()
                await _send_confirmation(data.email, token)
                return {"status": "ok", "campaign_slug": data.campaign_slug}

            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail="Already signed up for this campaign.",
            )

        token = str(uuid4()) if data.email else None
        signup = CampaignSignup(
            campaign_slug=data.campaign_slug,
            email=data.email,
            discord_id=data.discord_id,
            email_confirmed=False,
            confirmation_token=token,
        )
        db_session.add(signup)
        await db_session.commit()

        if data.email and token:
            await _send_confirmation(data.email, token)

        logger.info(
            "Campaign signup created: %s for %s",
            data.email or data.discord_id,
            data.campaign_slug,
        )

        return {"status": "ok", "campaign_slug": data.campaign_slug}
