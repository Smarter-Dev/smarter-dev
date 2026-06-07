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
from skrift.auth.second_factors.passkey_service import is_webauthn_available
from skrift.auth.second_factors.services import (
    deactivate_second_factor_enrollment,
    list_second_factor_enrollments_for_factor,
)
from skrift.config import get_settings as get_skrift_settings
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.user import User
from skrift.forms.core import verify_csrf
from skrift.lib.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.shared.config import get_settings
from smarter_dev.web.billing.portal import create_portal_session
from smarter_dev.web.models import SudoMembership, UserProfile

logger = logging.getLogger(__name__)


def _passkey_factor_key(skrift_settings) -> str | None:
    """First configured passkey second-factor key, or None if none set."""
    second_factors = skrift_settings.auth.second_factors
    for key in second_factors.get_method_keys():
        if second_factors.get_method_type(key) == "passkey":
            return key
    return None


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
    """Return the currently-active membership for ``user_id`` (or the
    most-recent non-revoked one if none is unexpired), else None.

    ``sudo_memberships`` is now an append-only history table: a user can
    accumulate rows from renewals / resubscribes / comps. The "active row"
    invariant lives here, not in a DB constraint.

    Preference order, all filtered by ``revoked_reason IS NULL``:
      1. Latest row with ``expires_at > now()``.
      2. Otherwise the most recently expired row (so the billing page can
         show "expired — renew at founder rate" instead of looking empty).
    """
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    result = await db_session.execute(
        select(SudoMembership)
        .where(SudoMembership.user_id == user_id)
        .where(SudoMembership.revoked_reason.is_(None))
        .order_by(
            (SudoMembership.expires_at > now).desc(),
            SudoMembership.expires_at.desc(),
        )
        .limit(1)
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

    @get("/security")
    async def security_page(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        user = await _current_user(request, db_session)

        oauth_result = await db_session.execute(
            select(OAuthAccount)
            .where(OAuthAccount.user_id == user.id)
            .order_by(OAuthAccount.created_at)
        )
        linked_accounts = list(oauth_result.scalars().all())

        skrift_settings = get_skrift_settings()
        factor_key = _passkey_factor_key(skrift_settings)
        passkeys: list = []
        if factor_key:
            passkeys = await list_second_factor_enrollments_for_factor(
                db_session, str(user.id), factor_key
            )

        return TemplateResponse(
            "account/security.html",
            context={
                "user": user,
                "active_tab": "security",
                "linked_accounts": linked_accounts,
                "passkeys": passkeys,
                "passkey_available": bool(factor_key) and is_webauthn_available(),
                "flash_messages": get_flash_messages(request),
            },
        )

    @post("/security/passkeys/{enrollment_id:uuid}/delete")
    async def delete_passkey(
        self, request: Request, db_session: AsyncSession, enrollment_id: UUID
    ) -> Redirect:
        user = await _current_user(request, db_session)
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path="/account/security")

        deactivated = await deactivate_second_factor_enrollment(
            db_session, user_id=user.id, enrollment_id=enrollment_id
        )
        await db_session.commit()
        if deactivated:
            flash_success(request, "Passkey removed.")
        return Redirect(path="/account/security")


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
        # Has the user linked a Discord OAuth account? Drives a small
        # "connect your Discord to receive in-server roles" prompt on the
        # billing card when they have an active membership but no link.
        discord_link_q = await db_session.execute(
            select(OAuthAccount.provider_account_id)
            .where(OAuthAccount.user_id == user.id)
            .where(OAuthAccount.provider == "discord")
            .limit(1)
        )
        has_discord_link = discord_link_q.first() is not None
        settings = get_settings()
        return TemplateResponse(
            "account/billing.html",
            context={
                "user": user,
                "membership": membership,
                "membership_expired": membership_expired,
                "has_discord_link": has_discord_link,
                "seats_total": settings.sudo_founder_seat_limit,
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
