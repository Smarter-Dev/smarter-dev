"""Custom page controllers for Smarter Dev.

Handles custom logic routes, redirects, and mounts the FastAPI API.
"""

import logging

from litestar import Controller, get
from litestar.handlers import asgi
from litestar.response import Redirect, Template
from litestar.types import Receive, Scope, Send
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import CampaignSignup

import smarter_dev.web.roles  # noqa: F401  — registers custom Skrift roles at import time

logger = logging.getLogger(__name__)


def _normalize_mounted_path(scope: Scope) -> None:
    """Normalize mounted sub-app paths without breaking explicit trailing slashes.

    Litestar can present the mounted path with a trailing slash even when the
    client requested a non-slash route. Preserve paths that the client actually
    requested with a trailing slash, since the mounted FastAPI app has
    ``redirect_slashes=False`` for strict route matching.
    """
    path = scope.get("path", "/")
    if path == "/" or not path.endswith("/"):
        return

    raw_path = scope.get("raw_path", b"")
    if isinstance(raw_path, (bytes, bytearray)):
        raw_path_text = raw_path.decode("latin-1")
    else:
        raw_path_text = str(raw_path or "")

    if raw_path_text.endswith("/"):
        return

    scope["path"] = path.rstrip("/")


class SudoController(Controller):
    """Handles sudo campaign routes."""

    path = "/sudo"

    @get("/confirm")
    async def confirm_signup(self, token: str, db_session: AsyncSession) -> Template:
        """Confirm a sudo waitlist email via token and render themed result."""
        result = await db_session.execute(
            select(CampaignSignup).where(
                CampaignSignup.confirmation_token == token
            )
        )
        signup = result.scalar_one_or_none()

        if signup:
            signup.email_confirmed = True
            signup.confirmation_token = None
            await db_session.commit()
            logger.info("Email confirmed for signup %s", signup.id)

        return Template(
            "page-sudo-confirm.html",
            context={"success": signup is not None},
        )


@get("/discord")
async def discord_redirect() -> Redirect:
    return Redirect("https://discord.gg/de8kajxbYS")


@asgi("/api", is_mount=True, copy_scope=True)
async def api_mount(scope: Scope, receive: Receive, send: Send) -> None:
    """Mount the FastAPI API as an ASGI sub-application."""
    from smarter_dev.web.api.app import api

    _normalize_mounted_path(scope)
    await api(scope, receive, send)


@asgi("/bot-admin", is_mount=True, copy_scope=True)
async def bot_admin_mount(scope: Scope, receive: Receive, send: Send) -> None:
    """Mount the legacy Starlette admin interface as an ASGI sub-application."""
    from smarter_dev.web.admin.app import create_admin_app

    _normalize_mounted_path(scope)
    await create_admin_app()(scope, receive, send)
