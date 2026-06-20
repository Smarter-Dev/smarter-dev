"""Admin controller for managing database-backed feature flags."""

from __future__ import annotations

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web import feature_flags as flags_service


# Known flags surfaced in the admin UI even before they've been touched at
# runtime. Listed here so the admin can pre-configure them.
SEEDED_FLAGS: tuple[tuple[str, str], ...] = (
    (
        "sudo_launch",
        "Gates the sudo pricing/checkout page. When off, the public waitlist page renders.",
    ),
)


class FeatureFlagsAdminController(Controller):
    """Feature flag management in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/feature-flags",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Feature Flags", "icon": "toggle-right", "order": 90},
    )
    async def feature_flags_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        await flags_service.ensure_flags(db_session, SEEDED_FLAGS)
        flags = await flags_service.list_flags(db_session)
        return TemplateResponse(
            "admin/feature_flags/list.html",
            context={
                "flags": flags,
                "modes": flags_service.VALID_MODES,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/feature-flags/{key:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def feature_flags_update(
        self, request: Request, db_session: AsyncSession, key: str
    ) -> Redirect:
        form = await request.form()
        mode = (form.get("mode") or "").strip()
        if mode not in flags_service.VALID_MODES:
            flash_error(
                request,
                f"Invalid mode {mode!r}; expected one of {flags_service.VALID_MODES}.",
            )
            return Redirect(path="/admin/feature-flags")

        await flags_service.set_mode(db_session, key, mode)
        flash_success(request, f"Feature flag '{key}' is now {mode}.")
        return Redirect(path="/admin/feature-flags")
