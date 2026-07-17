"""Native Litestar port of the guild-member management bot API.

Ports the legacy FastAPI ``routers/members.py`` (prefix
``/guilds/{guild_id}/members``) — part of unit U3 in
docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact path, verb, status
code, and request/response shape of the FastAPI implementation so
``smarter_dev/bot/client.py`` (and any external caller) needs zero changes:

- ``DELETE /api/guilds/{guild_id}/members/{user_id}`` → 200, ``SuccessResponse``.

Removes a user's squad memberships and bytes balance when they leave a guild;
transaction history is preserved for audit integrity. The operation is
idempotent (``GuildOperations.remove_user_data`` succeeds even with no rows).

Auth parity: the legacy endpoint guarded with ``verify_guild_access`` — a valid
key (no scope gate) plus a guild-id snowflake check (400 on a non-positive /
non-integer id). This port takes the base :data:`BOT_API_GUARDS`
(``Permission("bot-api")``) and reproduces the guild-id validation in-handler via
:func:`_validate_guild_id`, raising the same plain ``{"detail": "Invalid guild
ID"}`` 400.

Error-shape parity: the legacy handler caught ``DatabaseOperationError`` and
answered with ``security_utils.create_database_error`` — a plain body that hides
internal detail outside verbose development. :func:`errors.secure_database_error`
reproduces that exactly, so this handler catches ``DatabaseOperationError`` rather
than letting the flat ``handle_database_error`` shape fire.
"""

from __future__ import annotations

from datetime import datetime, timezone

from litestar import Controller, delete
from litestar.status_codes import HTTP_200_OK
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.schemas import SuccessResponse
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    plain_error,
    secure_database_error,
)
from smarter_dev.web.crud import DatabaseOperationError, GuildOperations

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


def _validate_guild_id(guild_id: str) -> None:
    """Reject a non-positive / non-integer guild id with the legacy 400.

    Mirrors ``dependencies.verify_guild_access``: a bare ``HTTPException(400,
    "Invalid guild ID")`` — a plain ``{"detail": "Invalid guild ID"}`` body.
    """
    try:
        if int(guild_id) <= 0:
            raise ValueError("Invalid guild ID")
    except ValueError:
        raise plain_error(400, "Invalid guild ID")


class MemberController(Controller):
    """Guild-member data cleanup on leave."""

    path = "/api/guilds/{guild_id:str}/members"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @delete("/{user_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def cleanup_member_data(
        self,
        db_session: AsyncSession,
        guild_id: str,
        user_id: str,
    ) -> SuccessResponse:
        """Remove a user's squads and bytes info when they leave a guild.

        Deletes squad memberships and bytes balance for the user in this guild.
        Transaction history is preserved for audit integrity.
        """
        _validate_guild_id(guild_id)
        try:
            operations = GuildOperations()
            await operations.remove_user_data(db_session, guild_id, user_id)
            await db_session.commit()
            return SuccessResponse(
                message=f"Cleaned up user {user_id} data in guild {guild_id}",
                timestamp=datetime.now(timezone.utc),
            )
        except DatabaseOperationError as database_error:
            raise secure_database_error(database_error)
