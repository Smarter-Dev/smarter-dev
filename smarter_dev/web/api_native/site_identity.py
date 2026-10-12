"""Authenticated server-held identity snapshots; no email-based account merging."""

from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import Response
from litestar import put
from litestar.exceptions import HTTPException
from pydantic import ValidationError
from skrift.auth.guards import Permission
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_billing import _billing_error_response
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.billing.converge import strip_discord_roles
from smarter_dev.web.site_identity import IdentitySnapshot
from smarter_dev.web.site_identity import provision_identity


class SiteIdentityController(Controller):
    path = "/api/site/identities"
    exception_handlers = {
        Exception: _billing_error_response,
        HTTPException: _billing_error_response,
    }

    @put("/{user_id:str}", guards=[*SITE_API_GUARDS, Permission("site-identity")])
    async def ensure(
        self, db_session: AsyncSession, user_id: str, data: dict[str, Any]
    ) -> Response:
        try:
            uid = UUID(user_id)
            if str(uid) != user_id.lower():
                raise ValueError
        except ValueError:
            raise plain_error(400, "Invalid account ID") from None
        try:
            snapshot = IdentitySnapshot.model_validate(data, strict=True)
        except ValidationError:
            raise plain_error(422, "Invalid identity snapshot") from None
        try:
            created, unlinked = await provision_identity(db_session, uid, snapshot)
            # Strip through the existing projection helper before committing
            # unlink. A transport/config failure rolls back the identity update,
            # leaving the durable Worker revision pending for an idempotent retry.
            for discord_id in unlinked:
                try:
                    await strip_discord_roles(discord_id, strict=True)
                except Exception:
                    raise plain_error(503, "Identity service unavailable") from None
            await db_session.commit()
        except IntegrityError:
            await db_session.rollback()
            raise plain_error(409, "identity_conflict") from None
        return Response(
            {"user_id": str(uid), "revision": snapshot.revision, "state": "ready"},
            status_code=201 if created else 200,
            headers={"Cache-Control": "private, no-store"},
        )
