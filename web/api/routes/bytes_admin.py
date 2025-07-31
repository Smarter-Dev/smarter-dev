"""Bytes system admin management API routes."""

from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field
import structlog

from web.database import get_db
from web.auth.api import verify_admin_api_key
from web.crud.bytes import bytes_crud, transaction_crud, config_crud
from web.services.redis_publisher import get_redis_publisher

router = APIRouter()
logger = structlog.get_logger()


class AdminBalanceAdjustment(BaseModel):
    """Request model for admin balance adjustment."""

    user_id: str = Field(..., description="User ID to adjust balance for")
    adjustment: int = Field(..., description="Amount to adjust (positive or negative)")
    reason: str = Field(..., description="Reason for the adjustment")
    reset_streak: bool = Field(
        default=False, description="Whether to reset daily streak"
    )


class AdminBulkOperation(BaseModel):
    """Request model for bulk operations."""

    user_ids: List[str] = Field(..., description="List of user IDs")
    operation: str = Field(..., description="Operation type")
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Operation parameters"
    )


class AdminConfigUpdate(BaseModel):
    """Request model for admin config updates."""

    starting_balance: Optional[int] = Field(None, ge=0, le=10000)
    daily_amount: Optional[int] = Field(None, ge=1, le=1000)
    max_transfer: Optional[int] = Field(None, ge=1, le=100000)
    cooldown_hours: Optional[int] = Field(None, ge=1, le=168)
    role_rewards: Optional[Dict[str, int]] = Field(None)
    enabled: Optional[bool] = Field(None)


class AdminUserCreate(BaseModel):
    """Request model for creating a user."""

    user_id: str = Field(..., description="Discord user ID")
    username: str = Field(..., description="Discord username")
    starting_balance: Optional[int] = Field(None, ge=0, le=10000)


@router.post("/guilds/{guild_id}/bytes/admin/adjust-balance")
async def admin_adjust_balance(
    guild_id: str,
    adjustment: AdminBalanceAdjustment,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Adjust a user's balance (admin only)."""
    try:
        # Get or create balance
        balance = await bytes_crud.get_or_create_balance(
            session, guild_id, adjustment.user_id
        )

        old_balance = balance.balance
        new_balance = max(0, old_balance + adjustment.adjustment)

        # Update balance
        balance.balance = new_balance
        if adjustment.reset_streak:
            balance.streak_count = 0
            balance.last_daily = None

        await session.commit()

        # Create admin transaction record
        await transaction_crud.create_transaction(
            session,
            guild_id=guild_id,
            giver_id="admin",
            giver_username="System Admin",
            receiver_id=adjustment.user_id,
            receiver_username="Unknown",
            amount=abs(adjustment.adjustment),
            reason=f"Admin adjustment: {adjustment.reason}",
            transaction_type="admin_adjustment",
        )

        # Publish update
        redis_publisher = get_redis_publisher()
        await redis_publisher.publish_balance_update(
            guild_id, adjustment.user_id, new_balance
        )

        logger.info(
            "Admin balance adjustment",
            guild_id=guild_id,
            user_id=adjustment.user_id,
            old_balance=old_balance,
            new_balance=new_balance,
            adjustment=adjustment.adjustment,
            reason=adjustment.reason,
        )

        return {
            "user_id": adjustment.user_id,
            "old_balance": old_balance,
            "new_balance": new_balance,
            "adjustment": adjustment.adjustment,
            "streak_reset": adjustment.reset_streak,
        }

    except Exception as e:
        logger.error(
            "Admin balance adjustment failed",
            guild_id=guild_id,
            user_id=adjustment.user_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to adjust balance: {str(e)}"
        )


@router.post("/guilds/{guild_id}/bytes/admin/bulk-adjust")
async def admin_bulk_adjust(
    guild_id: str,
    operation: AdminBulkOperation,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Perform bulk operations on user balances (admin only)."""
    try:
        results = []
        failed = []

        for user_id in operation.user_ids:
            try:
                if operation.operation == "adjust_balance":
                    adjustment = operation.parameters.get("adjustment", 0)
                    reason = operation.parameters.get("reason", "Bulk adjustment")

                    balance = await bytes_crud.get_or_create_balance(
                        session, guild_id, user_id
                    )
                    old_balance = balance.balance
                    balance.balance = max(0, old_balance + adjustment)

                    # Create transaction record
                    await transaction_crud.create_transaction(
                        session,
                        guild_id=guild_id,
                        giver_id="admin",
                        giver_username="System Admin",
                        receiver_id=user_id,
                        receiver_username="Unknown",
                        amount=abs(adjustment),
                        reason=f"Bulk operation: {reason}",
                        transaction_type="admin_bulk",
                    )

                    results.append(
                        {
                            "user_id": user_id,
                            "old_balance": old_balance,
                            "new_balance": balance.balance,
                            "success": True,
                        }
                    )

                elif operation.operation == "reset_streak":
                    balance = await bytes_crud.get_user_balance(
                        session, user_id, guild_id
                    )
                    if balance:
                        balance.streak_count = 0
                        balance.last_daily = None
                        results.append(
                            {
                                "user_id": user_id,
                                "streak_reset": True,
                                "success": True,
                            }
                        )
                    else:
                        failed.append({"user_id": user_id, "reason": "User not found"})

                else:
                    failed.append({"user_id": user_id, "reason": "Unknown operation"})

            except Exception as e:
                failed.append({"user_id": user_id, "reason": str(e)})

        await session.commit()

        # Publish bulk updates
        redis_publisher = get_redis_publisher()
        for result in results:
            if "new_balance" in result:
                await redis_publisher.publish_balance_update(
                    guild_id, result["user_id"], result["new_balance"]
                )

        logger.info(
            "Bulk admin operation completed",
            guild_id=guild_id,
            operation=operation.operation,
            total_users=len(operation.user_ids),
            successful=len(results),
            failed=len(failed),
        )

        return {
            "operation": operation.operation,
            "total_users": len(operation.user_ids),
            "successful": len(results),
            "failed": len(failed),
            "results": results,
            "failures": failed,
        }

    except Exception as e:
        logger.error(
            "Bulk admin operation failed",
            guild_id=guild_id,
            operation=operation.operation,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Bulk operation failed: {str(e)}")


@router.post("/guilds/{guild_id}/bytes/admin/force-daily")
async def admin_force_daily(
    guild_id: str,
    user_id: str,
    amount: Optional[int] = Query(None, description="Custom daily amount"),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Force award daily bytes to a user (admin only)."""
    try:
        # Get config for default amount
        config = await config_crud.get_config(session, guild_id)
        daily_amount = amount or config.daily_amount

        # Get or create balance
        balance = await bytes_crud.get_or_create_balance(session, guild_id, user_id)

        # Award daily bytes
        result = await bytes_crud.award_daily_bytes(
            session, guild_id, user_id, daily_amount, force=True
        )

        # Create transaction record
        await transaction_crud.create_transaction(
            session,
            guild_id=guild_id,
            giver_id="admin",
            giver_username="System Admin",
            receiver_id=user_id,
            receiver_username="Unknown",
            amount=daily_amount,
            reason="Admin forced daily award",
            transaction_type="admin_daily",
        )

        # Publish update
        redis_publisher = get_redis_publisher()
        await redis_publisher.publish_balance_update(
            guild_id, user_id, result["new_balance"]
        )

        logger.info(
            "Admin forced daily award",
            guild_id=guild_id,
            user_id=user_id,
            amount=daily_amount,
            new_balance=result["new_balance"],
        )

        return {
            "user_id": user_id,
            "amount_awarded": daily_amount,
            "new_balance": result["new_balance"],
            "new_streak": result["streak_count"],
        }

    except Exception as e:
        logger.error(
            "Admin force daily failed",
            guild_id=guild_id,
            user_id=user_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to force daily award: {str(e)}"
        )


@router.put("/guilds/{guild_id}/bytes/admin/config")
async def admin_update_config(
    guild_id: str,
    config_updates: AdminConfigUpdate,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Update guild bytes configuration (admin only)."""
    try:
        # Get existing config
        config = await config_crud.get_config(session, guild_id)

        # Apply updates
        update_data = config_updates.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if hasattr(config, field):
                setattr(config, field, value)

        await session.commit()

        # Publish config update
        redis_publisher = get_redis_publisher()
        await redis_publisher.publish_config_update(guild_id, update_data)

        logger.info(
            "Admin config update",
            guild_id=guild_id,
            updates=update_data,
        )

        return {
            "guild_id": guild_id,
            "updated_fields": list(update_data.keys()),
            "new_config": {
                "starting_balance": config.starting_balance,
                "daily_amount": config.daily_amount,
                "max_transfer": config.max_transfer,
                "cooldown_hours": config.cooldown_hours,
                "role_rewards": config.role_rewards,
                "enabled": config.enabled,
            },
        }

    except Exception as e:
        logger.error(
            "Admin config update failed",
            guild_id=guild_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to update config: {str(e)}"
        )


@router.delete("/guilds/{guild_id}/bytes/admin/user/{user_id}")
async def admin_delete_user(
    guild_id: str,
    user_id: str,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Delete a user's bytes data (admin only)."""
    try:
        # Get user balance
        balance = await bytes_crud.get_user_balance(session, user_id, guild_id)
        if not balance:
            raise HTTPException(status_code=404, detail="User not found")

        deleted_balance = balance.balance

        # Delete balance and transactions
        await bytes_crud.delete_user_data(session, guild_id, user_id)

        logger.info(
            "Admin user deletion",
            guild_id=guild_id,
            user_id=user_id,
            deleted_balance=deleted_balance,
        )

        return {
            "user_id": user_id,
            "deleted_balance": deleted_balance,
            "success": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Admin user deletion failed",
            guild_id=guild_id,
            user_id=user_id,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {str(e)}")


@router.post("/guilds/{guild_id}/bytes/admin/user")
async def admin_create_user(
    guild_id: str,
    user_data: AdminUserCreate,
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Create a new user with specific balance (admin only)."""
    try:
        # Check if user already exists
        existing = await bytes_crud.get_user_balance(
            session, user_data.user_id, guild_id
        )
        if existing:
            raise HTTPException(status_code=400, detail="User already exists")

        # Get config for default starting balance
        config = await config_crud.get_config(session, guild_id)
        starting_balance = user_data.starting_balance or config.starting_balance

        # Create balance
        balance = await bytes_crud.get_or_create_balance(
            session, guild_id, user_data.user_id, starting_balance=starting_balance
        )

        # Create transaction record if non-default balance
        if starting_balance != config.starting_balance:
            await transaction_crud.create_transaction(
                session,
                guild_id=guild_id,
                giver_id="admin",
                giver_username="System Admin",
                receiver_id=user_data.user_id,
                receiver_username=user_data.username,
                amount=starting_balance,
                reason="Admin user creation with custom balance",
                transaction_type="admin_create",
            )

        logger.info(
            "Admin user creation",
            guild_id=guild_id,
            user_id=user_data.user_id,
            username=user_data.username,
            starting_balance=starting_balance,
        )

        return {
            "user_id": user_data.user_id,
            "username": user_data.username,
            "starting_balance": starting_balance,
            "success": True,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Admin user creation failed",
            guild_id=guild_id,
            user_id=user_data.user_id,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")


@router.get("/guilds/{guild_id}/bytes/admin/audit")
async def admin_get_audit_log(
    guild_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Get audit log of admin actions (admin only)."""
    try:
        # Get admin transactions
        admin_transactions = await transaction_crud.get_admin_transactions(
            session, guild_id, limit=limit, offset=offset
        )

        total_count = await transaction_crud.count_admin_transactions(session, guild_id)

        formatted_transactions = []
        for txn in admin_transactions:
            formatted_transactions.append(
                {
                    "id": txn.id,
                    "timestamp": txn.created_at.isoformat(),
                    "action": txn.transaction_type,
                    "target_user": txn.receiver_id,
                    "amount": txn.amount,
                    "reason": txn.reason,
                    "admin_user": txn.giver_username,
                }
            )

        return {
            "audit_log": formatted_transactions,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    except Exception as e:
        logger.error(
            "Admin audit log failed",
            guild_id=guild_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to get audit log: {str(e)}"
        )


@router.post("/guilds/{guild_id}/bytes/admin/reset-guild")
async def admin_reset_guild(
    guild_id: str,
    confirm: bool = Query(..., description="Must be true to confirm reset"),
    session: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_api_key),
) -> Dict[str, Any]:
    """Reset all bytes data for a guild (admin only) - DESTRUCTIVE."""
    if not confirm:
        raise HTTPException(
            status_code=400, detail="Must confirm reset with confirm=true"
        )

    try:
        # Get counts before deletion
        balance_count = await bytes_crud.count_users(session, guild_id)
        transaction_count = await transaction_crud.count_transactions(session, guild_id)

        # Reset all guild data
        await bytes_crud.reset_guild_data(session, guild_id)

        logger.warning(
            "Admin guild reset performed",
            guild_id=guild_id,
            deleted_balances=balance_count,
            deleted_transactions=transaction_count,
        )

        return {
            "guild_id": guild_id,
            "deleted_balances": balance_count,
            "deleted_transactions": transaction_count,
            "reset_completed": True,
            "warning": "All bytes data for this guild has been permanently deleted",
        }

    except Exception as e:
        logger.error(
            "Admin guild reset failed",
            guild_id=guild_id,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Failed to reset guild: {str(e)}")
