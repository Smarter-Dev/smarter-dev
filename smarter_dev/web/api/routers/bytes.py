"""Bytes economy endpoints for the Smarter Dev API.

This module provides REST API endpoints for managing the bytes economy system
including balances, transactions, daily claims, and configurations.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Path, Query, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.services.streak_service import StreakService
from smarter_dev.shared.date_provider import get_date_provider
from smarter_dev.web.api.dependencies import (
    get_database_session,
    verify_bot_token,
    verify_guild_access,
    get_request_metadata
)
from smarter_dev.web.api.exceptions import (
    create_validation_error,
    create_not_found_error,
    create_conflict_error,
    validate_discord_id
)
from smarter_dev.web.api.schemas import (
    BytesBalanceResponse,
    BytesTransactionCreate,
    BytesTransactionResponse,
    DailyClaimResponse,
    BytesConfigResponse,
    BytesConfigUpdate,
    LeaderboardResponse,
    TransactionHistoryResponse,
    SuccessResponse,
    ErrorResponse,
    ValidationErrorResponse,
)
from smarter_dev.web.crud import (
    BytesOperations,
    BytesConfigOperations,
    DatabaseOperationError,
    NotFoundError,
    ConflictError
)

router = APIRouter()


@router.get(
    "/balance/{user_id}", 
    response_model=BytesBalanceResponse,
    responses={
        200: {"description": "User balance retrieved successfully"},
        400: {"model": ErrorResponse, "description": "Invalid user ID format"},
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Access forbidden"},
        404: {"model": ErrorResponse, "description": "User balance not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def get_balance(
    request: Request,
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> BytesBalanceResponse:
    """Get user's bytes balance.
    
    Returns the current bytes balance for a specific user in the guild,
    including their streak information and transaction totals.
    """
    # Validate user ID format
    validate_discord_id(user_id, "user ID")
    
    bytes_ops = BytesOperations()
    balance = await bytes_ops.get_balance(db, guild_id, user_id)
    return BytesBalanceResponse.model_validate(balance)


@router.post("/daily/{user_id}", response_model=DailyClaimResponse)
async def claim_daily(
    request: Request,
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> DailyClaimResponse:
    """Claim daily bytes reward with streak calculation.
    
    Claims the daily bytes reward for a user. Calculates streak bonuses
    based on consecutive daily claims and applies the appropriate multiplier.
    """
    # Validate user ID format
    validate_discord_id(user_id, "user ID")
    
    bytes_ops = BytesOperations()
    config_ops = BytesConfigOperations()
    streak_service = StreakService(date_provider=get_date_provider())
    
    # Get guild configuration
    try:
        config = await config_ops.get_config(db, guild_id)
    except NotFoundError:
        config = await config_ops.create_config(db, guild_id)
    
    # Get current balance
    balance = await bytes_ops.get_balance(db, guild_id, user_id)
    
    # Use StreakService to calculate complete streak result
    streak_result = streak_service.calculate_streak_result(
        last_daily=balance.last_daily,
        current_streak=balance.streak_count,
        daily_amount=config.daily_amount,
        streak_bonuses=config.streak_bonuses
    )
    
    # Check if user can claim today
    if not streak_result.can_claim:
        raise create_conflict_error(
            "Daily reward has already been claimed today. Try again tomorrow!",
            request
        )
    
    # Get current UTC date for database storage
    current_utc_date = get_date_provider().today()
    
    # Update balance with daily reward using calculated values
    updated_balance = await bytes_ops.update_daily_reward(
        db, 
        guild_id, 
        user_id, 
        config.daily_amount, 
        streak_result.streak_bonus,
        streak_result.new_streak_count,
        current_utc_date
    )
    
    # Calculate next claim time (midnight UTC tomorrow)
    next_claim_at = datetime.combine(
        streak_result.next_claim_date, 
        datetime.min.time()
    ).replace(tzinfo=timezone.utc)
    
    await db.commit()
    
    return DailyClaimResponse(
        balance=BytesBalanceResponse.model_validate(updated_balance),
        reward_amount=streak_result.reward_amount,
        streak_bonus=streak_result.streak_bonus,
        next_claim_at=next_claim_at
    )


@router.post("/transactions", response_model=BytesTransactionResponse)
async def create_transaction(
    request: Request,
    transaction: BytesTransactionCreate,
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> BytesTransactionResponse:
    """Create a bytes transaction between users.
    
    Transfers bytes from one user to another within the guild.
    Validates transfer limits and prevents self-transfers.
    """
    bytes_ops = BytesOperations()
    config_ops = BytesConfigOperations()
    
    # Get guild configuration for transfer limits
    try:
        config = await config_ops.get_config(db, guild_id)
    except NotFoundError:
        config = await config_ops.create_config(db, guild_id)
    
    # Check transfer amount against max limit
    if transaction.amount > config.max_transfer:
        raise create_validation_error(
            f"Transfer amount ({transaction.amount}) exceeds maximum limit of {config.max_transfer} bytes",
            "amount",
            request
        )
    
    # Prevent self-transfer
    if transaction.giver_id == transaction.receiver_id:
        raise create_validation_error(
            "Cannot transfer bytes to yourself",
            "receiver_id",
            request
        )
    
    # Create the transaction
    created_transaction = await bytes_ops.create_transaction(
        db,
        guild_id,
        transaction.giver_id,
        transaction.giver_username,
        transaction.receiver_id,
        transaction.receiver_username,
        transaction.amount,
        transaction.reason
    )
    
    await db.commit()
    return BytesTransactionResponse.model_validate(created_transaction)


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    limit: int = Query(10, ge=1, le=100, description="Number of top users to return"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> LeaderboardResponse:
    """Get guild bytes leaderboard.
    
    Returns the top users by bytes balance in descending order.
    Useful for displaying competitive rankings within the guild.
    """
    bytes_ops = BytesOperations()
    top_balances = await bytes_ops.get_leaderboard(db, guild_id, limit)
    users = [BytesBalanceResponse.model_validate(balance) for balance in top_balances]
    
    return LeaderboardResponse(
        guild_id=guild_id,
        users=users,
        total_users=len(users),
        generated_at=datetime.now(timezone.utc)
    )


@router.get("/transactions", response_model=TransactionHistoryResponse)
async def get_transactions(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    limit: int = Query(20, ge=1, le=100, description="Number of transactions to return"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> TransactionHistoryResponse:
    """Get transaction history for guild or user.
    
    Returns recent transaction history, optionally filtered by a specific user.
    Transactions are returned in reverse chronological order (newest first).
    """
    # Validate user_id if provided
    if user_id:
        validate_discord_id(user_id, "User ID")
    
    bytes_ops = BytesOperations()
    transactions = await bytes_ops.get_transaction_history(db, guild_id, user_id, limit)
    transaction_responses = [
        BytesTransactionResponse.model_validate(tx) for tx in transactions
    ]
    
    return TransactionHistoryResponse(
        guild_id=guild_id,
        transactions=transaction_responses,
        total_count=len(transactions),
        user_id=user_id
    )


@router.get("/config", response_model=BytesConfigResponse)
async def get_config(
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> BytesConfigResponse:
    """Get guild bytes configuration.
    
    Returns the current bytes economy configuration for the guild.
    Creates default configuration if none exists.
    """
    config_ops = BytesConfigOperations()
    try:
        config = await config_ops.get_config(db, guild_id)
    except NotFoundError:
        config = await config_ops.create_config(db, guild_id)
        await db.commit()
    
    return BytesConfigResponse.model_validate(config)


@router.put("/config", response_model=BytesConfigResponse)
async def update_config(
    request: Request,
    config_update: BytesConfigUpdate,
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> BytesConfigResponse:
    """Update guild bytes configuration.
    
    Updates the bytes economy settings for the guild.
    Only provided fields will be updated, others remain unchanged.
    """
    config_ops = BytesConfigOperations()
    update_data = config_update.model_dump(exclude_unset=True, exclude_none=True)
    
    if not update_data:
        raise create_validation_error(
            "No configuration updates provided. At least one field must be specified.",
            request=request
        )
    
    updated_config = await config_ops.update_config(db, guild_id, **update_data)
    await db.commit()
    
    return BytesConfigResponse.model_validate(updated_config)


@router.delete("/config", response_model=SuccessResponse)
async def delete_config(
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> SuccessResponse:
    """Delete guild bytes configuration.
    
    Removes the bytes economy configuration for the guild.
    This will reset to default settings when accessed again.
    """
    config_ops = BytesConfigOperations()
    await config_ops.delete_config(db, guild_id)
    await db.commit()
    
    return SuccessResponse(
        message=f"Bytes configuration deleted for guild {guild_id}",
        timestamp=datetime.now(timezone.utc)
    )


@router.post("/reset-streak/{user_id}", response_model=BytesBalanceResponse)
async def reset_streak(
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    token: str = Depends(verify_bot_token),
    metadata: dict = Depends(get_request_metadata)
) -> BytesBalanceResponse:
    """Reset user's daily claim streak.
    
    Resets the user's daily claim streak to zero.
    This is typically used for administrative purposes or penalty enforcement.
    """
    # Validate user ID format
    validate_discord_id(user_id, "user ID")
    
    bytes_ops = BytesOperations()
    updated_balance = await bytes_ops.reset_streak(db, guild_id, user_id)
    await db.commit()
    
    return BytesBalanceResponse.model_validate(updated_balance)