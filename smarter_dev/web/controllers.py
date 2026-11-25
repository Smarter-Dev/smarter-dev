"""Custom page controllers for Smarter Dev.

Handles custom logic routes and redirects. The bot API lives in native
Litestar controllers under ``smarter_dev.web.api_native``.
"""

import logging

from litestar import Controller, Request, get, post
from litestar.exceptions import NotFoundException
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.db.models.user import User

from smarter_dev.shared.config import get_settings
from smarter_dev.web import feature_flags as flags_service
from smarter_dev.web.billing.checkout import (
    CheckoutError,
    UnknownRole,
    create_checkout_session,
)
from smarter_dev.web.feature_flags_admin import SEEDED_FLAGS
from smarter_dev.web.models import CampaignSignup

import smarter_dev.web.roles  # noqa: F401  — registers custom Skrift roles at import time
import smarter_dev.web.hooks_sudo  # noqa: F401  — registers Skrift action hooks for sudo converge
import smarter_dev.web.account_hooks  # noqa: F401  — registers account page section hooks

logger = logging.getLogger(__name__)


# Description used when sudo_launch is auto-created on first read. Centralised
# here so the admin UI shows useful copy without a manual seed step.
_SUDO_LAUNCH_DESCRIPTION = dict(SEEDED_FLAGS).get("sudo_launch")


class SudoController(Controller):
    """Handles the /sudo landing, founder checkout, and confirmation flows."""

    path = "/sudo"

    @get("/")
    async def landing(
        self, request: Request, db_session: AsyncSession
    ) -> Template:
        """Render either the pricing page or the waitlist landing.

        Resolves the ``sudo_launch`` feature flag against the requesting user:
        admins see the pricing page when the flag is in ``admin_only`` mode,
        everyone sees it when ``enabled``, and the original waitlist landing
        renders for everyone else.
        """
        show_pricing = await flags_service.is_enabled(
            db_session,
            "sudo_launch",
            request,
            description=_SUDO_LAUNCH_DESCRIPTION,
        )

        if not show_pricing:
            return Template("page-sudo.html")

        faqs = [
            {
                "q": "What is sudo?",
                "a": "sudo is elevated access on top of the free Smarter Dev community. The community stays free. sudo is how the work that costs money gets funded, and how you get more out of it: every RunHacks challenge as a Hacker, and an inside seat as a Founder.",
            },
            {
                "q": "Is the community still free?",
                "a": "Yes. Discord and the community stay free, same as always. sudo is a layer on top, not a paywall pulled across what\u2019s already here.",
            },
            {
                "q": "Is Hacker a subscription?",
                "a": "Yes. Hacker is $8 a month, billed monthly. Cancel anytime, no commitment.",
            },
            {
                "q": "Is Founder recurring?",
                "a": "No. Founder is a one-time payment. $256, or more if you want to. No auto-renewal, no second charge.",
            },
            {
                "q": "What does Founder get that Hacker doesn\u2019t?",
                "a": "Everything a Hacker gets, plus an early look at what\u2019s being built, a voice while it\u2019s young, and a Founder role with a dedicated channel. An inside seat while the rest gets built.",
            },
            {
                "q": "What if you can\u2019t ship Gym or the Lab?",
                "a": "You keep everything you already have. RunHacks is live now, that\u2019s what the money buys, and it\u2019s not going anywhere. Gym and the Lab are next, and I\u2019m building them full-time. I won\u2019t name a date I might miss, but I\u2019m not raising money to decide whether to build this. I\u2019ve run this community for years and just left my job to finish the rest. If it takes longer than I want, you\u2019ve still got the challenges and the community you came for.",
            },
            {
                "q": "Refund policy?",
                "a": "Founder has a 14-day refund, no questions. Hacker is monthly, so cancel anytime and you keep access through the month you paid for.",
            },
            {
                "q": "Can I start as a Hacker and go Founder later?",
                "a": "Yes. Start monthly, fund the build whenever it makes sense for you.",
            },
        ]

        return Template(
            "sudo/pricing.html",
            context={"faqs": faqs},
        )

    @post("/checkout", guards=[auth_guard])
    async def checkout(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        """Start a Polar Checkout for the requested offering (role)."""
        # The flag must let this user see the pricing page; otherwise the
        # button shouldn't even be reachable.
        show_pricing = await flags_service.is_enabled(
            db_session,
            "sudo_launch",
            request,
            description=_SUDO_LAUNCH_DESCRIPTION,
        )
        if not show_pricing:
            raise NotFoundException("Page not found")

        form = await request.form()
        role = (form.get("role") or "").strip()
        if role not in {"hacker", "founder"}:
            return Redirect(path="/sudo?checkout_error=1")

        user_id = request.session.get("user_id") if request.session else None
        if not user_id:
            # auth_guard normally handles this — defensive.
            return Redirect(path=f"/auth/login?next=/sudo")

        result = await db_session.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return Redirect(path="/auth/login?next=/sudo")

        settings = get_settings()
        base = settings.site_base_url.rstrip("/")

        try:
            checkout_url = await create_checkout_session(
                db_session,
                user,
                role=role,
                success_url=f"{base}/sudo/checkout/success",
            )
        except UnknownRole:
            return Redirect(path="/sudo?checkout_error=1")
        except CheckoutError:
            logger.exception("Failed to create Checkout Session for role %r.", role)
            return Redirect(path="/sudo?checkout_error=1")

        return Redirect(path=checkout_url, status_code=303)

    @get("/checkout/success")
    async def checkout_success(self, request: Request) -> Template:
        """Land here after a successful Polar Checkout."""
        return Template("sudo/checkout_success.html")

    @get("/checkout/cancel")
    async def checkout_cancel(self, request: Request) -> Template:
        """Land here when the user abandons checkout."""
        return Template("sudo/checkout_cancel.html")

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
