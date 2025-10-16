"""Guild member management endpoints.

Provides endpoints for actions related to guild members, such as cleanup
when a user leaves a guild.
"""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Path, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import (
    get_database_session,
    APIKey,
    verify_guild_access,
    get_request_metadata,
)
from smarter_dev.web.api.security_utils import (
    create_database_error,
)
from smarter_dev.web.api.schemas import SuccessResponse
from smarter_dev.web.crud import GuildOperations, DatabaseOperationError


router = APIRouter(prefix="/guilds/{guild_id}/members", tags=["Members"])


@router.delete("/{user_id}", response_model=SuccessResponse)
async def cleanup_member_data(
    api_key: APIKey,
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata),
):
    """Remove a user's squads and bytes info when they leave a guild.

    Deletes squad memberships and bytes balance for the user in this guild.
    Transaction history is preserved for audit integrity.
    """
    try:
        ops = GuildOperations()
        await ops.remove_user_data(db, guild_id, user_id)
        await db.commit()

        return SuccessResponse(
            message=f"Cleaned up user {user_id} data in guild {guild_id}",
            timestamp=datetime.now(timezone.utc),
        )
    except DatabaseOperationError as e:
        raise create_database_error(e)
