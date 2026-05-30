"""User account controllers: profile editing and subscription management."""

from __future__ import annotations

import logging
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.db.models.user import User
from skrift.lib.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.shared.config import get_settings
from smarter_dev.web.billing.portal import create_portal_session
from smarter_dev.web.models import SudoMembership, UserProfile

logger = logging.getLogger(__name__)


async def _current_user(request: Request, db_session: AsyncSession) -> User:
    user_id = request.session.get("user_id") if request.session else None
    if not user_id:
        raise NotAuthorizedException()
    result = await db_session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotAuthorizedException()
    return user


async def _get_or_create_profile(
    db_session: AsyncSession, user_id: UUID
) -> UserProfile:
    result = await db_session.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        profile = UserProfile(user_id=user_id)
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
    return profile


async def _get_active_membership(
    db_session: AsyncSession, user_id: UUID
) -> SudoMembership | None:
    result = await db_session.execute(
        select(SudoMembership).where(SudoMembership.user_id == user_id)
    )
    return result.scalar_one_or_none()


class AccountController(Controller):
    """Profile tab of the user-facing account page."""

    path = "/account"
    guards = [auth_guard]

    @get("/")
    async def profile(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        user = await _current_user(request, db_session)
        profile = await _get_or_create_profile(db_session, user.id)
        return TemplateResponse(
            "account/profile.html",
            context={
                "user": user,
                "profile": profile,
                "active_tab": "profile",
                "flash_messages": get_flash_messages(request),
            },
        )

    @post("/")
    async def update_profile(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        user = await _current_user(request, db_session)
        form = await request.form()

        display_name = (form.get("display_name") or "").strip() or None
        handle = (form.get("handle") or "").strip() or None
        bio = (form.get("bio") or "").strip() or None
        timezone = (form.get("timezone") or "").strip() or None

        if handle and len(handle) > 40:
            flash_error(request, "Handle is too long (max 40 characters).")
            return Redirect(path="/account")
        if bio and len(bio) > 500:
            flash_error(request, "Bio is too long (max 500 characters).")
            return Redirect(path="/account")

        # Uniqueness check on handle (case-sensitive match; DB UNIQUE backs it up).
        if handle:
            result = await db_session.execute(
                select(UserProfile).where(
                    UserProfile.handle == handle,
                    UserProfile.user_id != user.id,
                )
            )
            if result.scalar_one_or_none() is not None:
                flash_error(request, "That handle is already taken.")
                return Redirect(path="/account")

        profile = await _get_or_create_profile(db_session, user.id)
        profile.handle = handle
        profile.bio = bio
        profile.timezone = timezone

        if display_name is not None:
            user.name = display_name

        await db_session.commit()
        flash_success(request, "Profile saved.")
        return Redirect(path="/account")


class BillingController(Controller):
    """Billing & Plan tab of the user-facing account page."""

    path = "/account/billing"
    guards = [auth_guard]

    @get("/")
    async def show(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        user = await _current_user(request, db_session)
        membership = await _get_active_membership(db_session, user.id)
        membership_expired = False
        if membership and membership.expires_at:
            from datetime import datetime, timezone as _tz
            membership_expired = membership.expires_at < datetime.now(_tz.utc)
        return TemplateResponse(
            "account/billing.html",
            context={
                "user": user,
                "membership": membership,
                "membership_expired": membership_expired,
                "active_tab": "billing",
                "flash_messages": get_flash_messages(request),
            },
        )

    @post("/portal")
    async def open_portal(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        user = await _current_user(request, db_session)
        membership = await _get_active_membership(db_session, user.id)
        if membership is None:
            flash_error(request, "You don't have a membership yet.")
            return Redirect(path="/account/billing")

        settings = get_settings()
        return_url = f"{settings.site_base_url.rstrip('/')}/account/billing"

        try:
            url = create_portal_session(
                membership.stripe_customer_id, return_url=return_url
            )
        except Exception:
            logger.exception("Failed to open Stripe Customer Portal session.")
            flash_error(request, "Couldn't open billing portal — try again shortly.")
            return Redirect(path="/account/billing")

        return Redirect(path=url, status_code=303)
