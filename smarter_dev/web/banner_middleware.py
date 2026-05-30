"""ASGI middleware that resolves the ``sudo_launch`` flag for every page.

The founder announcement banner is rendered site-wide in ``base.html`` behind
this flag, but there is no global template context in this app and the flag is
async + DB-backed. This middleware resolves it once per request (the flag layer
keeps a 30s in-process cache) and stashes the result on
``scope["state"]["sudo_launch"]`` so templates can read ``request.state``.
"""

from __future__ import annotations

import logging

from litestar.connection import Request
from litestar.types import ASGIApp, Receive, Scope, Send

from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web import feature_flags
from smarter_dev.web.feature_flags_admin import SEEDED_FLAGS

logger = logging.getLogger(__name__)

_SUDO_LAUNCH_DESCRIPTION = dict(SEEDED_FLAGS).get("sudo_launch")

# Prefixes that never render the site chrome — skip the flag lookup for them.
_SKIP_PREFIXES = ("/api", "/static", "/bot-admin", "/_")


class SudoLaunchBannerMiddleware:
    """Attach the resolved ``sudo_launch`` state to ``scope["state"]``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        active = False
        try:
            request: Request = Request(scope, receive)
            async with get_skrift_db_session_context() as session:
                active = await feature_flags.is_enabled(
                    session,
                    "sudo_launch",
                    request,
                    description=_SUDO_LAUNCH_DESCRIPTION,
                )
        except Exception:  # never let the banner break a page render
            logger.warning("sudo_launch banner flag lookup failed", exc_info=True)

        scope.setdefault("state", {})["sudo_launch"] = active
        await self.app(scope, receive, send)
