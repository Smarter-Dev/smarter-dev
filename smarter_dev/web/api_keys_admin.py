"""API key management controller for the Skrift admin panel."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.lib.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.crud import APIKeyOperations


class APIKeyAdminController(Controller):
    """API key management in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/api-keys",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "API Keys", "icon": "key", "order": 30},
    )
    async def api_keys_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List all API keys."""
        ctx = await get_admin_context(request, db_session)
        ops = APIKeyOperations()
        api_keys = await ops.list_api_keys(db_session, include_inactive=True)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/api-keys/list.html",
            context={
                "flash_messages": flash_messages,
                "api_keys": api_keys,
                **ctx,
            },
        )

    @get(
        "/api-keys/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def api_keys_create_form(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show the create API key form."""
        ctx = await get_admin_context(request, db_session)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/api-keys/create.html",
            context={
                "flash_messages": flash_messages,
                **ctx,
            },
        )

    @post(
        "/api-keys",
        guards=[auth_guard, Permission("administrator")],
    )
    async def api_keys_create(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> TemplateResponse:
        """Create a new API key."""
        ctx = await get_admin_context(request, db_session)

        name = data.get("name", "").strip()
        description = data.get("description", "").strip() or None
        rate_limit = int(data.get("rate_limit_per_hour", 1000) or 1000)

        # Handle scopes — may be a single string or a list
        raw_scopes = data.get("scopes", [])
        if isinstance(raw_scopes, str):
            scopes = [raw_scopes] if raw_scopes else []
        elif isinstance(raw_scopes, list):
            scopes = [s for s in raw_scopes if s]
        else:
            scopes = []

        if not name:
            flash_error(request, "Name is required.")
            return Redirect(path="/admin/api-keys/create")

        if not scopes:
            flash_error(request, "At least one scope is required.")
            return Redirect(path="/admin/api-keys/create")

        try:
            ops = APIKeyOperations()
            created_by = ctx["user"].name or ctx["user"].email or str(ctx["user"].id)
            api_key, plaintext_key = await ops.create_api_key(
                session=db_session,
                name=name,
                scopes=scopes,
                created_by=created_by,
                rate_limit_per_hour=rate_limit,
            )

            # Set description if provided
            if description:
                api_key.description = description
                await db_session.commit()

            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/api-keys/created.html",
                context={
                    "flash_messages": flash_messages,
                    "api_key": api_key,
                    "plaintext_key": plaintext_key,
                    **ctx,
                },
            )
        except Exception as e:
            flash_error(request, f"Failed to create API key: {e}")
            return Redirect(path="/admin/api-keys/create")

    @post(
        "/api-keys/{key_id:uuid}/revoke",
        guards=[auth_guard, Permission("administrator")],
    )
    async def api_keys_revoke(
        self,
        request: Request,
        db_session: AsyncSession,
        key_id: UUID,
    ) -> Redirect:
        """Revoke an API key."""
        try:
            ops = APIKeyOperations()
            revoked = await ops.revoke_api_key(db_session, key_id)
            if revoked:
                flash_success(request, "API key revoked.")
            else:
                flash_error(request, "API key not found.")
        except Exception as e:
            flash_error(request, f"Failed to revoke API key: {e}")
        return Redirect(path="/admin/api-keys")
