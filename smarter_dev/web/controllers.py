"""Custom page controllers for Smarter Dev.

Handles custom logic routes, redirects, and mounts the FastAPI API.
"""

import logging

from litestar import Controller, Request, get, post
from litestar.exceptions import NotFoundException
from litestar.handlers import asgi
from litestar.response import Redirect, Template
from litestar.types import Receive, Scope, Send
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.db.models.user import User

from smarter_dev.shared.config import get_settings
from smarter_dev.web import feature_flags as flags_service
from smarter_dev.web.billing.pricing_context import build_pricing_context
from smarter_dev.web.billing.checkout import (
    CheckoutError,
    FounderSeatsExhausted,
    UnknownTier,
    create_founder_checkout_session,
)
from smarter_dev.web.feature_flags_admin import SEEDED_FLAGS
from smarter_dev.web.models import CampaignSignup

import smarter_dev.web.roles  # noqa: F401  — registers custom Skrift roles at import time
import smarter_dev.web.hooks_sudo  # noqa: F401  — registers Skrift action hooks for sudo converge

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

        pricing_ctx = await build_pricing_context(db_session)
        seats_total = pricing_ctx["seats_total"]
        tiers = pricing_ctx["tiers"]
        tier_map = {t["id"]: t for t in tiers}

        def _join_prices(values: list[int], conj: str) -> str:
            parts = [f"${v}" for v in values]
            if len(parts) <= 1:
                return "".join(parts)
            return f"{', '.join(parts[:-1])}, {conj} {parts[-1]}"

        annual_list = _join_prices([t["annual"] for t in tiers], "or")
        monthly_list = _join_prices([t["public_monthly"] for t in tiers], "and")

        return Template(
            "sudo/pricing.html",
            context={
                **pricing_ctx,
                "tier_map": tier_map,
                "founder_principles": [
                    {
                        "head": "One payment buys the year",
                        "body": f"{annual_list} once — a year of access, no subscription, no auto-renewal. We don't touch the card again unless you ask us to.",
                    },
                    {
                        "head": "Renew at the founder rate",
                        "body": "When the year is up, renewing keeps you at 33% off the public price. The discount you bought in at is the discount you keep.",
                    },
                    {
                        "head": f"rwx 0day is capped at {seats_total} seats",
                        "body": f"r-- and rw- stay open. rwx 0day is a one-time founder window — when the {seats_total} seats are gone, the price and the preview access are gone with them.",
                    },
                ],
                "roadmap": [
                    {
                        "head": "DAY ONE",
                        "eta": "live at sudo open",
                        "body": "What every founder gets when sudo opens. The Resources Agent answers questions from our library of top resources, RunHacks drops new challenges on a regular schedule, and the founder role carries your access into everything we ship next.",
                        "bullets": [
                            "Resources Agent answers using our selection of top resources",
                            "RunHacks drops new challenges regularly",
                            "Founder role and members-only Discord channels",
                        ],
                    },
                    {
                        "head": "GYM",
                        "eta": "shipping next",
                        "body": "Lesson-based curriculum paired with an AI tutor that remembers you. It tracks where you've gotten stuck and adapts how it walks you through what's next.",
                        "bullets": [
                            "Tutor that remembers where you've struggled",
                            "Stack-trace-first; no syntax tutorials",
                            "Each lesson builds on the last",
                        ],
                    },
                    {
                        "head": "THE LAB",
                        "eta": "after Gym",
                        "body": "Guided agentic coding environments. Using real coding agents, run in cloud-hosted workspaces accessible from any browser, with the lesson sitting alongside the agent terminal.",
                        "bullets": [
                            "Cloud workspaces, no local setup",
                            "Lesson + agent terminal, side-by-side",
                            "First look at every platform feature",
                        ],
                    },
                ],
                "faqs": [
                    {
                        "q": "What is sudo, and why are you launching paid tiers now?",
                        "a": "Smarter Dev has always been free, and the community side stays that way. sudo is how we ship the things we couldn't build for free: a tutor with memory, agentic learning environments, deeper tools for working alongside AI. The world has changed in a way that asks more of developers, and we want to help developers like us meet that. We're opening founder tiers now because that work takes more than we can do for free.",
                    },
                    {
                        "q": "What happens to the free Smarter Dev community?",
                        "a": "It stays free. Discord access, RunHacks basic challenges, and Gym previews stay open. sudo is a layer added on top, not a paywall pulled across.",
                    },
                    {
                        "q": "Where does my annual tier's money actually go?",
                        "a": "Cloud infrastructure to run the website, Discord bot, RunHacks, and the agentic workspaces in the Lab. Development time on Gym curriculum and the Lab environments.",
                    },
                    {
                        "q": "What if you can't ship Gym or the Lab on time?",
                        "a": "Honest answer: it's a risk we all take. We've scoped Gym and the Lab to what we believe we can build in Q3 and Q4 with founder runway, but software ships when it's ready. If we slip, we slip publicly and your year of access keeps running. The refund policy applies for the first 14 days and prorates after that.",
                    },
                    {
                        "q": "Is this a subscription?",
                        "a": "No. Each year is a one-time purchase. No auto-renewal, no surprise charges. Public monthly, when it launches, is a normal subscription; founder is a deliberately different deal. One year up front, no recurring bill, and your founder pricing is waiting if you come back.",
                    },
                    {
                        "q": "What happens at the end of my year?",
                        "a": "Your access ends 12 months after you reserve your seat. Come back within 30 days and your founder pricing is waiting: same 33% off the public rate, same founder role and channel access. Wait longer than 30 days and you re-enter at the public rate.",
                    },
                    {
                        "q": "What if I want to pay monthly?",
                        "a": f"Wait until the official launch. Currently, we're fundraising to prove we have the demand and cash to operate at a larger scale. Public monthly opens at {monthly_list}, no founder pricing. If saving a third on the year and keeping founder pricing when you come back sounds better, annual is open right now.",
                    },
                    {
                        "q": "Refund policy?",
                        "a": "14 days, no questions, full refund. After that, a prorated refund on the remaining months. Your seat releases back to the pool.",
                    },
                    {
                        "q": "What if I want to support Smarter Dev beyond a membership?",
                        "a": "<p>sudo memberships are how most people support what we're building, but they fund a platform that's bigger than any single tier. Smarter Dev is the platform; sudo is the membership layer that pays for it. If you want to back Smarter Dev directly, beyond what a membership covers, email me at <a href=\"mailto:hello@smarter.dev\">hello@smarter.dev</a> and we'll work out what fits.</p><p>We shout out supporters who go above and beyond, on the Discord and the website, if they want the recognition. Plenty of people would rather keep it quiet, and that's just as welcome. Either way it goes straight into the build.</p>",
                    },
                ],
            },
        )

    @post("/checkout", guards=[auth_guard])
    async def checkout_founder(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        """Start a Stripe Checkout Session for the requested founder tier."""
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
        tier = (form.get("tier") or "").strip()
        if tier not in {"r", "rw", "rwx"}:
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
            checkout_url = await create_founder_checkout_session(
                db_session,
                user,
                tier=tier,
                success_url=f"{base}/sudo/checkout/success",
                cancel_url=f"{base}/sudo/checkout/cancel",
            )
        except FounderSeatsExhausted:
            return Redirect(path="/sudo?sold_out=1")
        except UnknownTier:
            return Redirect(path="/sudo?checkout_error=1")
        except CheckoutError:
            logger.exception("Failed to create founder Checkout Session for tier %r.", tier)
            return Redirect(path="/sudo?checkout_error=1")

        return Redirect(path=checkout_url, status_code=303)

    @get("/checkout/success")
    async def checkout_success(self, request: Request) -> Template:
        """Land here after a successful Stripe Checkout."""
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
