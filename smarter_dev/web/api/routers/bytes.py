"""Bytes economy endpoints for the Smarter Dev API.

This module provides REST API endpoints for managing the bytes economy system
including balances, transactions, daily claims, and configurations.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Path, Query, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.services.streak_service import StreakService
from smarter_dev.shared.date_provider import get_date_provider
from smarter_dev.web.api.dependencies import (
    get_database_session,
    APIKey,
    apply_rate_limiting,
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
    DailyClaimRequest,
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
    response: Response,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> BytesBalanceResponse:
    """Get user's bytes balance.
    
    Returns the current bytes balance for a specific user in the guild,
    including their streak information and transaction totals.
    """
    # Validate user ID format
    validate_discord_id(user_id, "user ID")
    
    bytes_ops = BytesOperations()
    balance = await bytes_ops.get_or_create_balance(db, guild_id, user_id)
    return BytesBalanceResponse.model_validate(balance)


@router.post("/daily", response_model=DailyClaimResponse)
async def claim_daily(
    request: Request,
    response: Response,
    claim_request: DailyClaimRequest,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> DailyClaimResponse:
    """Claim daily bytes reward with streak calculation.
    
    Claims the daily bytes reward for a user. Calculates streak bonuses
    based on consecutive daily claims and applies the appropriate multiplier.
    """
    user_id = claim_request.user_id
    username = claim_request.username or f"User {user_id}"
    
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
    
    # Get or create balance (starts with 0 for new users)
    try:
        balance = await bytes_ops.get_balance(db, guild_id, user_id)
        # Check if this is a new user (never claimed daily before)
        is_new_user = balance.last_daily is None
    except NotFoundError:
        # Create new user with 0 balance - they'll get their "starting balance" as their first daily reward
        from smarter_dev.web.models import BytesBalance
        balance = BytesBalance(
            guild_id=guild_id,
            user_id=user_id,
            balance=0,
            total_received=0
        )
        db.add(balance)
        await db.flush()  # Ensure timestamps are populated
        is_new_user = True
    
    # Use StreakService to calculate streak result (works for both new and returning users)
    # For new users, this will be their first "daily" claim which gives them starting balance
    streak_result = streak_service.calculate_streak_result(
        last_daily=balance.last_daily,
        current_streak=balance.streak_count,
        daily_amount=config.starting_balance if is_new_user else config.daily_amount,
        streak_bonuses=config.streak_bonuses
    )
    
    # Check if user can claim today (works for both new and returning users)
    if not streak_result.can_claim:
        raise create_conflict_error(
            "Daily reward has already been claimed today. Try again tomorrow!",
            request
        )
    
    # Get current UTC date for database storage
    current_utc_date = get_date_provider().today()
    
    # Determine the reward amount (starting balance for new users, daily amount for returning users)
    reward_amount = config.starting_balance if is_new_user else config.daily_amount
    
    # Update balance with reward using calculated values
    updated_balance = await bytes_ops.update_daily_reward(
        db, 
        guild_id, 
        user_id, 
        username,
        reward_amount, 
        streak_result.streak_bonus,
        streak_result.new_streak_count,
        current_utc_date,
        is_new_member=is_new_user
    )
    
    # Calculate next claim time (midnight UTC tomorrow)
    next_claim_at = datetime.combine(
        streak_result.next_claim_date, 
        datetime.min.time()
    ).replace(tzinfo=timezone.utc)
    
    # Refresh the balance to ensure all attributes are loaded
    await db.refresh(updated_balance)
    
    # Serialize model before commit to avoid session detachment issues
    balance_response = BytesBalanceResponse.model_validate(updated_balance)
    
    await db.commit()
    
    return DailyClaimResponse(
        balance=balance_response,
        reward_amount=streak_result.reward_amount,
        streak_bonus=streak_result.streak_bonus,
        next_claim_at=next_claim_at
    )


@router.post("/transactions", response_model=BytesTransactionResponse)
async def create_transaction(
    request: Request,
    response: Response,
    transaction: BytesTransactionCreate,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    
    # Check transfer cooldown if configured
    if config.transfer_cooldown_hours > 0:
        from datetime import datetime, timezone, timedelta
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(hours=config.transfer_cooldown_hours)
        
        # Check for recent transfers sent by this user (not received by them)
        # This prevents daily rewards and received transfers from triggering cooldown
        recent_transfers = await bytes_ops.get_sent_transaction_history(
            db,
            guild_id,
            sender_user_id=transaction.giver_id,
            limit=1
        )
        
        if recent_transfers and recent_transfers[0].created_at > cooldown_cutoff:
            cooldown_end_time = recent_transfers[0].created_at + timedelta(hours=config.transfer_cooldown_hours)
            time_remaining = (cooldown_end_time - datetime.now(timezone.utc)).total_seconds()
            hours_remaining = int(time_remaining // 3600)
            minutes_remaining = int((time_remaining % 3600) // 60)
            
            # Include both human-readable text and machine-readable timestamp
            cooldown_message = f"Transfer cooldown active. You can send bytes again in {hours_remaining}h {minutes_remaining}m.|{int(cooldown_end_time.timestamp())}"
            raise create_validation_error(cooldown_message, "cooldown", request)
    
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
    
    # Serialize model before commit to avoid session detachment issues
    transaction_response = BytesTransactionResponse.model_validate(created_transaction)
    
    await db.commit()
    return transaction_response


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    request: Request,
    response: Response,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    limit: int = Query(10, ge=1, le=100, description="Number of top users to return"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    request: Request,
    response: Response,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    limit: int = Query(20, ge=1, le=100, description="Number of transactions to return"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    request: Request,
    response: Response,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    response: Response,
    config_update: BytesConfigUpdate,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    
    # Serialize model before commit to avoid session detachment issues
    config_response = BytesConfigResponse.model_validate(updated_config)
    
    await db.commit()
    
    return config_response


@router.delete("/config", response_model=SuccessResponse)
async def delete_config(
    request: Request,
    response: Response,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    request: Request,
    response: Response,
    api_key: APIKey,
    rate_limit_check: None = Depends(apply_rate_limiting),
    user_id: str = Path(..., description="Discord user ID"),
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_database_session),
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
    
    # Serialize model before commit to avoid session detachment issues
    balance_response = BytesBalanceResponse.model_validate(updated_balance)
    
    await db.commit()
    
    return balance_response