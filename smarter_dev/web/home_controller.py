"""Homepage controller.

Replaces Skrift's ``WebController`` for the ``/`` route so the homepage can
resolve the ``sudo_launch`` feature flag and render the founder pricing teaser
with live Stripe data. The CMS page catch-all (``view_page``) is inherited
unchanged.
"""

from __future__ import annotations

from litestar import Request, get
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.controllers.helpers import get_user_context
from skrift.controllers.web import WebController

from smarter_dev.web import feature_flags as flags_service
from smarter_dev.web.billing.pricing_context import build_pricing_context
from smarter_dev.web.controllers import _SUDO_LAUNCH_DESCRIPTION


class HomeController(WebController):
    """Serves a dynamic homepage; inherits Skrift's CMS page catch-all."""

    @get("/")
    async def index(
        self, request: "Request", db_session: AsyncSession
    ) -> Template:
        """Home page: founder pricing teaser when ``sudo_launch`` is on, else waitlist."""
        user_ctx = await get_user_context(request, db_session)
        flash = request.session.pop("flash", None)

        show_pricing = await flags_service.is_enabled(
            db_session,
            "sudo_launch",
            request,
            description=_SUDO_LAUNCH_DESCRIPTION,
        )

        context = {"flash": flash, "show_pricing": show_pricing, **user_ctx}
        if show_pricing:
            context.update(await build_pricing_context(db_session))

        return Template("index.html", context=context)
