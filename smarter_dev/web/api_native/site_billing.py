"""Site service facade over Python-owned sudo pricing and Polar billing."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.exceptions import HTTPException
from litestar.exceptions.responses import create_exception_response
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from skrift.auth.guards import Permission
from skrift.auth.services import get_user_permissions
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.user import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.billing_urls import billing_url_origin
from smarter_dev.shared.config import get_settings
from smarter_dev.web import feature_flags
from smarter_dev.web.account_controller import _get_active_membership
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.billing.checkout import UnknownRole
from smarter_dev.web.billing.checkout import create_checkout_session
from smarter_dev.web.billing.portal import create_portal_session
from smarter_dev.web.billing.pricing_context import build_pricing_context
from smarter_dev.web.chat.entitlements import resolve_spend_tier

SITE_BILLING_GUARDS = [*SITE_API_GUARDS, Permission("site-billing")]


def _billing_error_response(request: Request, exc: Exception) -> Response:
    """Keep existing error shapes, adding cache protection on billing only.

    Litestar bypasses controller response_headers for exception responses.
    Resolve the existing API handler by exception MRO, just as Litestar does,
    and use its standard sanitized response for HTTP/unexpected exceptions.
    """
    handler = next(
        (
            BOT_API_EXCEPTION_HANDLERS[cls]
            for cls in type(exc).__mro__
            if cls in BOT_API_EXCEPTION_HANDLERS
        ),
        create_exception_response,
    )
    response = handler(request, exc)
    response.headers["Cache-Control"] = "private, no-store"
    return response


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=128)
    success_url: str = Field(min_length=1, max_length=2083)
    cancel_url: str = Field(min_length=1, max_length=2083)


class PortalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    return_url: str | None = Field(default=None, min_length=1, max_length=2083)


def _site_url(value: str) -> str:
    """Accept only an exact configured billing origin, never a suffix match."""
    try:
        origin = billing_url_origin(value)
    except ValueError:
        raise plain_error(400, "Invalid billing return URL") from None
    settings = get_settings()
    try:
        configured = settings.site_billing_allowed_origins
        allowed = (
            {billing_url_origin(settings.site_base_url)}
            if configured is None
            else {billing_url_origin(url, origin_only=True) for url in configured}
        )
    except ValueError:
        raise plain_error(503, "Billing unavailable") from None
    if origin not in allowed:
        raise plain_error(400, "Return URLs must use an allowed billing origin")
    return value


async def _user(session: AsyncSession, user_id: str) -> User:
    try:
        uid = UUID(user_id)
        if str(uid) != user_id.lower():
            raise ValueError
    except ValueError:
        raise plain_error(400, "Invalid account ID") from None
    user = await session.scalar(
        select(User).where(User.id == uid, User.is_active.is_(True))
    )
    if user is None:
        raise plain_error(404, "Account not found")
    return user


async def _discord_linked(session: AsyncSession, user_id: UUID) -> bool:
    """Same check as the Python billing page's "Connect Discord" prompt."""
    linked = await session.scalar(
        select(OAuthAccount.provider_account_id)
        .where(OAuthAccount.user_id == user_id)
        .where(OAuthAccount.provider == "discord")
        .limit(1)
    )
    return linked is not None


async def _launched(session: AsyncSession) -> bool:
    # This catalog is public/cacheable at the Worker. Never resolve admin_only
    # against the service key owner's permissions or a browser-supplied identity.
    return await feature_flags.get_mode(session, "sudo_launch") == "enabled"


class SiteBillingController(Controller):
    path = "/api/site/billing"
    exception_handlers = {
        **dict.fromkeys(BOT_API_EXCEPTION_HANDLERS, _billing_error_response),
        HTTPException: _billing_error_response,
        Exception: _billing_error_response,
    }
    response_headers = {"Cache-Control": "private, no-store"}

    @get("/catalog", guards=SITE_BILLING_GUARDS)
    async def catalog(self, db_session: AsyncSession) -> dict[str, Any]:
        if not await _launched(db_session):
            return {
                "launched": False,
                "offerings": [],
                "offerings_by_role": {},
                "hacker": None,
                "founder": None,
            }
        try:
            context = await build_pricing_context(db_session)
        except Exception:
            raise plain_error(503, "Billing unavailable") from None
        return {"launched": True, **context}

    @get("/users/{user_id:str}/membership", guards=SITE_BILLING_GUARDS)
    async def membership(
        self, db_session: AsyncSession, user_id: str
    ) -> dict[str, Any]:
        user = await _user(db_session, user_id)
        membership = await _get_active_membership(db_session, user.id)
        # Chat runs on the member's roles (internal r/rw/rwx included), not
        # the plan, so the site gets Python's own tier rather than guessing.
        account = {
            "chat_tier": resolve_spend_tier(
                await get_user_permissions(db_session, user.id)
            ),
            "discord_linked": await _discord_linked(db_session, user.id),
        }
        if membership is None:
            return {
                "status": "none",
                "plan": None,
                "expires_at": None,
                "will_renew": None,
                "portal_available": False,
                "started_at": None,
                "amount_paid_minor": None,
                "currency": None,
                "refunded": None,
                **account,
            }
        expiry = membership.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        started = membership.purchased_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return {
            "status": "active" if expiry > datetime.now(UTC) else "expired",
            "plan": membership.role,
            "expires_at": expiry,
            "will_renew": membership.will_renew,
            "portal_available": bool(membership.customer_id)
            and membership.source != "comp",
            # As the Python billing page shows them; it prices in USD only.
            "started_at": started,
            "amount_paid_minor": membership.amount_paid_cents,
            "currency": "USD",
            "refunded": membership.refunded_at is not None,
            **account,
        }

    @post("/users/{user_id:str}/checkout", guards=SITE_BILLING_GUARDS, status_code=200)
    async def checkout(
        self, db_session: AsyncSession, user_id: str, data: CheckoutRequest
    ) -> dict[str, str]:
        user = await _user(db_session, user_id)
        success_url = _site_url(data.success_url)
        cancel_url = _site_url(data.cancel_url)
        if not await _launched(db_session):
            raise plain_error(404, "Billing not launched")
        try:
            pricing = await build_pricing_context(db_session)
        except Exception:
            raise plain_error(503, "Billing unavailable") from None
        offering = next(
            (
                o
                for o in pricing["offerings"]
                if data.plan_id in (o["id"], o["price_id"])
            ),
            None,
        )
        if offering is None:
            raise plain_error(400, "Unknown sudo offering")
        try:
            url = await create_checkout_session(
                db_session,
                user,
                role=offering["role"],
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except UnknownRole:
            raise plain_error(400, "Unknown sudo offering") from None
        except Exception:
            raise plain_error(503, "Billing unavailable") from None
        return {"url": url}

    @post("/users/{user_id:str}/portal", guards=SITE_BILLING_GUARDS, status_code=200)
    async def portal(
        self, db_session: AsyncSession, user_id: str, data: PortalRequest | None = None
    ) -> dict[str, str]:
        user = await _user(db_session, user_id)
        return_url = (
            _site_url(data.return_url) if data and data.return_url is not None else None
        )
        membership = await _get_active_membership(db_session, user.id)
        if (
            membership is None
            or not membership.customer_id
            or membership.source == "comp"
        ):
            raise plain_error(404, "No billing portal available")
        try:
            url = await create_portal_session(
                membership.customer_id,
                **({"return_url": return_url} if return_url is not None else {}),
            )
        except Exception:
            raise plain_error(503, "Billing unavailable") from None
        return {"url": url}
