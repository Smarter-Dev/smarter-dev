"""Pydantic schemas for bytes system API."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


# Request schemas
class BytesDailyRequest(BaseModel):
    """Request model for awarding daily bytes."""

    user_id: str = Field(..., description="Discord user ID")
    username: str = Field(..., max_length=100, description="Discord username")


class BytesTransferRequest(BaseModel):
    """Request model for transferring bytes."""

    giver_id: str = Field(..., description="Discord user ID of sender")
    giver_username: str = Field(
        ..., max_length=100, description="Discord username of sender"
    )
    receiver_id: str = Field(..., description="Discord user ID of receiver")
    receiver_username: str = Field(
        ..., max_length=100, description="Discord username of receiver"
    )
    amount: int = Field(..., gt=0, description="Amount of bytes to transfer")
    reason: Optional[str] = Field(
        None, max_length=500, description="Optional reason for transfer"
    )


class BytesConfigUpdateRequest(BaseModel):
    """Request model for updating guild bytes configuration."""

    starting_balance: Optional[int] = Field(
        None, ge=0, description="Starting balance for new users"
    )
    daily_amount: Optional[int] = Field(
        None, ge=1, description="Base daily reward amount"
    )
    max_transfer: Optional[int] = Field(
        None, ge=1, description="Maximum transfer amount"
    )
    cooldown_hours: Optional[int] = Field(
        None, ge=1, le=72, description="Hours between daily claims"
    )
    role_rewards: Optional[Dict[str, int]] = Field(
        None, description="Role ID to bytes threshold mapping"
    )


# Response schemas
class BytesBalanceResponse(BaseModel):
    """Response model for user balance."""

    balance: int = Field(..., description="Current balance")
    total_received: int = Field(..., description="Total bytes ever received")
    total_sent: int = Field(..., description="Total bytes ever sent")
    last_daily: Optional[str] = Field(
        None, description="Last daily claim date (ISO format)"
    )
    streak_count: int = Field(..., description="Current daily streak")
    daily_available: Optional[bool] = Field(
        None, description="Whether daily reward is available"
    )


class BytesDailyResponse(BaseModel):
    """Response model for daily reward."""

    amount_awarded: int = Field(..., description="Amount awarded")
    new_balance: int = Field(..., description="New total balance")
    streak_count: int = Field(..., description="New streak count")
    multiplier: int = Field(..., description="Streak multiplier applied")
    multiplier_display: Optional[str] = Field(
        None, description="Human-readable multiplier name"
    )


class BytesTransferResponse(BaseModel):
    """Response model for transfer."""

    transaction_id: str = Field(..., description="Unique transaction ID")
    giver_new_balance: int = Field(..., description="Sender's new balance")
    receiver_new_balance: int = Field(..., description="Receiver's new balance")
    giver_total_sent: int = Field(..., description="Sender's total sent")
    receiver_total_received: int = Field(..., description="Receiver's total received")
    success: bool = Field(True, description="Transfer success status")


class BytesLeaderboardEntry(BaseModel):
    """Single leaderboard entry."""

    user_id: str = Field(..., description="Discord user ID")
    username: str = Field(..., description="Discord username")
    balance: int = Field(..., description="Current balance")
    total_received: int = Field(..., description="Total bytes received")
    total_sent: int = Field(..., description="Total bytes sent")
    rank: int = Field(..., description="Leaderboard position")


class BytesLeaderboardResponse(BaseModel):
    """Response model for leaderboard."""

    leaderboard: List[BytesLeaderboardEntry] = Field(
        ..., description="Ordered leaderboard entries"
    )


class BytesTransactionEntry(BaseModel):
    """Single transaction entry."""

    id: str = Field(..., description="Transaction ID")
    guild_id: str = Field(..., description="Guild ID")
    giver_id: str = Field(..., description="Sender user ID")
    giver_username: str = Field(..., description="Sender username")
    receiver_id: str = Field(..., description="Receiver user ID")
    receiver_username: str = Field(..., description="Receiver username")
    amount: int = Field(..., description="Amount transferred")
    reason: Optional[str] = Field(None, description="Transfer reason")
    created_at: str = Field(..., description="Transaction timestamp (ISO format)")


class BytesTransactionHistoryResponse(BaseModel):
    """Response model for transaction history."""

    transactions: List[BytesTransactionEntry] = Field(
        ..., description="Transaction history"
    )
    total_count: Optional[int] = Field(
        None, description="Total transactions (if paginated)"
    )


class BytesConfigResponse(BaseModel):
    """Response model for guild configuration."""

    starting_balance: int = Field(..., description="Starting balance for new users")
    daily_amount: int = Field(..., description="Base daily reward amount")
    max_transfer: int = Field(..., description="Maximum transfer amount")
    cooldown_hours: int = Field(..., description="Hours between daily claims")
    role_rewards: Dict[str, int] = Field(
        default_factory=dict, description="Role rewards configuration"
    )


class BytesStatsResponse(BaseModel):
    """Response model for guild statistics."""

    total_users: int = Field(..., description="Total users with bytes")
    total_bytes: int = Field(..., description="Total bytes in circulation")
    total_transactions: int = Field(..., description="Total number of transactions")
    average_balance: float = Field(..., description="Average user balance")
    top_balance: Optional[int] = Field(None, description="Highest balance")
    daily_awards_today: Optional[int] = Field(
        None, description="Daily awards claimed today"
    )


# Error schemas
class BytesErrorResponse(BaseModel):
    """Error response model."""

    error: str = Field(..., description="Error message")
    code: Optional[str] = Field(None, description="Error code")
    details: Optional[Dict[str, Any]] = Field(
        None, description="Additional error details"
    )


# Admin schemas (for admin interface)
class AdminBalanceAdjustment(BaseModel):
    """Admin balance adjustment request."""

    user_id: str = Field(..., description="Target user ID")
    guild_id: str = Field(..., description="Guild ID")
    amount: int = Field(..., description="Amount to add/subtract (can be negative)")
    reason: str = Field(..., max_length=200, description="Reason for adjustment")


class AdminBulkOperation(BaseModel):
    """Admin bulk operation request."""

    user_ids: List[str] = Field(..., description="List of user IDs")
    guild_id: str = Field(..., description="Guild ID")
    operation: str = Field(..., description="Operation type")
    parameters: Dict[str, Any] = Field(
        default_factory=dict, description="Operation parameters"
    )


class AdminUserDetail(BaseModel):
    """Detailed user information for admin interface."""

    user_id: str = Field(..., description="User ID")
    balances: List[Dict[str, Any]] = Field(
        ..., description="User balances across guilds"
    )
    recent_transactions: List[BytesTransactionEntry] = Field(
        ..., description="Recent transactions"
    )
    flags: Optional[List[str]] = Field(None, description="User flags or notes")
