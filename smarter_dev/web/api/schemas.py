"""Pydantic schemas for API request and response models.

This module defines all the request and response schemas used by the FastAPI
endpoints, providing proper validation, serialization, and documentation.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict, field_validator, field_serializer


class BaseAPIModel(BaseModel):
    """Base model for all API schemas with common configuration."""
    
    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        arbitrary_types_allowed=False,
    )


# ============================================================================
# Authentication Schemas
# ============================================================================

class TokenResponse(BaseAPIModel):
    """Response model for token validation."""
    
    valid: bool = Field(description="Whether the token is valid")
    expires_at: Optional[datetime] = Field(None, description="Token expiration time")
    
    @field_serializer('expires_at')
    def serialize_expires_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


# ============================================================================
# Bytes Economy Schemas
# ============================================================================

class BytesBalanceResponse(BaseAPIModel):
    """Response model for user bytes balance."""
    
    guild_id: str = Field(description="Discord guild ID")
    user_id: str = Field(description="Discord user ID")
    balance: int = Field(ge=0, description="Current bytes balance")
    total_received: int = Field(ge=0, description="Total bytes received")
    total_sent: int = Field(ge=0, description="Total bytes sent")
    streak_count: int = Field(ge=0, description="Daily claim streak count")
    last_daily: Optional[date] = Field(None, description="Last daily claim date")
    created_at: datetime = Field(description="Record creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")
    
    @field_serializer('last_daily')
    def serialize_last_daily(self, value: Optional[date]) -> Optional[str]:
        return value.isoformat() if value else None
    
    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class BytesTransactionCreate(BaseAPIModel):
    """Request model for creating a bytes transaction."""
    
    giver_id: str = Field(description="Discord ID of the giver")
    giver_username: str = Field(min_length=1, max_length=100, description="Username of the giver")
    receiver_id: str = Field(description="Discord ID of the receiver")
    receiver_username: str = Field(min_length=1, max_length=100, description="Username of the receiver")
    amount: int = Field(gt=0, le=10000, description="Amount to transfer")
    reason: Optional[str] = Field(None, max_length=200, description="Optional reason for transfer")
    
    @field_validator('giver_id', 'receiver_id')
    @classmethod
    def validate_discord_id(cls, v: str) -> str:
        """Validate Discord snowflake ID format."""
        try:
            id_int = int(v)
            if id_int <= 0:
                raise ValueError("ID must be positive")
            return v
        except ValueError:
            raise ValueError("Invalid Discord ID format")


class BytesTransactionResponse(BaseAPIModel):
    """Response model for bytes transaction."""
    
    id: UUID = Field(description="Transaction ID")
    guild_id: str = Field(description="Discord guild ID")
    giver_id: str = Field(description="Giver Discord ID")
    giver_username: str = Field(description="Giver username")
    receiver_id: str = Field(description="Receiver Discord ID")
    receiver_username: str = Field(description="Receiver username")
    amount: int = Field(description="Transfer amount")
    reason: Optional[str] = Field(description="Transfer reason")
    created_at: datetime = Field(description="Transaction timestamp")
    
    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat()


class DailyClaimRequest(BaseAPIModel):
    """Request model for daily bytes claim."""
    
    user_id: str = Field(description="Discord user ID claiming daily")
    
    @field_validator('user_id')
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """Validate Discord user ID."""
        try:
            user_id_int = int(v)
            if user_id_int <= 0:
                raise ValueError("User ID must be positive")
            return v
        except ValueError:
            raise ValueError("Invalid Discord user ID format")


class DailyClaimResponse(BaseAPIModel):
    """Response model for daily claim result."""
    
    balance: BytesBalanceResponse = Field(description="Updated balance")
    reward_amount: int = Field(description="Amount awarded")
    streak_bonus: int = Field(description="Streak multiplier applied")
    next_claim_at: datetime = Field(description="When next claim is available")
    
    @field_serializer('next_claim_at')
    def serialize_next_claim_at(self, value: datetime) -> str:
        return value.isoformat()


class BytesConfigResponse(BaseAPIModel):
    """Response model for guild bytes configuration."""
    
    guild_id: str = Field(description="Discord guild ID")
    daily_amount: int = Field(ge=1, description="Base daily reward amount")
    starting_balance: int = Field(ge=0, description="Starting balance for new users")
    max_transfer: int = Field(ge=1, description="Maximum transfer amount")
    daily_cooldown_hours: int = Field(ge=1, le=48, description="Hours between daily claims")
    streak_bonuses: Dict[int, int] = Field(description="Streak day to multiplier mapping")
    transfer_tax_rate: float = Field(ge=0.0, le=1.0, description="Tax rate on transfers")
    is_enabled: bool = Field(description="Whether bytes system is enabled")
    created_at: datetime = Field(description="Configuration creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")
    
    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class BytesConfigUpdate(BaseAPIModel):
    """Request model for updating bytes configuration."""
    
    daily_amount: Optional[int] = Field(None, ge=1, le=1000, description="Daily reward amount")
    starting_balance: Optional[int] = Field(None, ge=0, le=10000, description="Starting balance")
    max_transfer: Optional[int] = Field(None, ge=1, le=100000, description="Maximum transfer")
    daily_cooldown_hours: Optional[int] = Field(None, ge=1, le=48, description="Daily cooldown")
    streak_bonuses: Optional[Dict[int, int]] = Field(None, description="Streak bonuses")
    transfer_tax_rate: Optional[float] = Field(None, ge=0.0, le=1.0, description="Transfer tax")
    is_enabled: Optional[bool] = Field(None, description="Enable/disable system")


class LeaderboardResponse(BaseAPIModel):
    """Response model for bytes leaderboard."""
    
    guild_id: str = Field(description="Discord guild ID")
    users: List[BytesBalanceResponse] = Field(description="Top users by balance")
    total_users: int = Field(description="Total users in guild")
    generated_at: datetime = Field(description="Leaderboard generation time")
    
    @field_serializer('generated_at')
    def serialize_generated_at(self, value: datetime) -> str:
        return value.isoformat()


class TransactionHistoryResponse(BaseAPIModel):
    """Response model for transaction history."""
    
    guild_id: str = Field(description="Discord guild ID")
    transactions: List[BytesTransactionResponse] = Field(description="Transaction list")
    total_count: int = Field(description="Total transaction count")
    user_id: Optional[str] = Field(None, description="User filter if applied")


# ============================================================================
# Squad Management Schemas
# ============================================================================

class SquadResponse(BaseAPIModel):
    """Response model for squad information."""
    
    id: UUID = Field(description="Squad unique ID")
    guild_id: str = Field(description="Discord guild ID")
    role_id: str = Field(description="Discord role ID")
    name: str = Field(description="Squad name")
    description: Optional[str] = Field(description="Squad description")
    max_members: Optional[int] = Field(description="Maximum member limit")
    switch_cost: int = Field(description="Cost to join squad")
    member_count: int = Field(description="Current member count")
    is_active: bool = Field(description="Whether squad is active")
    created_at: datetime = Field(description="Squad creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")
    
    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class SquadCreate(BaseAPIModel):
    """Request model for creating a squad."""
    
    role_id: str = Field(description="Discord role ID for squad")
    name: str = Field(min_length=1, max_length=100, description="Squad name")
    description: Optional[str] = Field(None, max_length=500, description="Squad description")
    max_members: Optional[int] = Field(None, ge=1, le=1000, description="Member limit")
    switch_cost: int = Field(ge=0, le=10000, default=50, description="Cost to join")
    
    @field_validator('role_id')
    @classmethod
    def validate_role_id(cls, v: str) -> str:
        """Validate Discord role ID format."""
        try:
            role_id_int = int(v)
            if role_id_int <= 0:
                raise ValueError("Role ID must be positive")
            return v
        except ValueError:
            raise ValueError("Invalid Discord role ID format")


class SquadUpdate(BaseAPIModel):
    """Request model for updating a squad."""
    
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="Squad name")
    description: Optional[str] = Field(None, max_length=500, description="Description")
    max_members: Optional[int] = Field(None, ge=1, le=1000, description="Member limit")
    switch_cost: Optional[int] = Field(None, ge=0, le=10000, description="Join cost")
    is_active: Optional[bool] = Field(None, description="Active status")


class SquadMembershipResponse(BaseAPIModel):
    """Response model for squad membership."""
    
    squad_id: UUID = Field(description="Squad ID")
    user_id: str = Field(description="Discord user ID")
    guild_id: str = Field(description="Discord guild ID")
    joined_at: datetime = Field(description="Membership start time")
    squad: SquadResponse = Field(description="Squad information")
    
    @field_serializer('joined_at')
    def serialize_joined_at(self, value: datetime) -> str:
        return value.isoformat()


class SquadJoinRequest(BaseAPIModel):
    """Request model for joining a squad."""
    
    user_id: str = Field(description="Discord user ID")
    
    @field_validator('user_id')
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """Validate Discord user ID."""
        try:
            user_id_int = int(v)
            if user_id_int <= 0:
                raise ValueError("User ID must be positive")
            return v
        except ValueError:
            raise ValueError("Invalid Discord user ID format")


class SquadLeaveRequest(BaseAPIModel):
    """Request model for leaving a squad."""
    
    user_id: str = Field(description="Discord user ID")
    
    @field_validator('user_id')
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """Validate Discord user ID."""
        try:
            user_id_int = int(v)
            if user_id_int <= 0:
                raise ValueError("User ID must be positive")
            return v
        except ValueError:
            raise ValueError("Invalid Discord user ID format")


class SquadMembersResponse(BaseAPIModel):
    """Response model for squad member list."""
    
    squad: SquadResponse = Field(description="Squad information")
    members: List[SquadMembershipResponse] = Field(description="Squad members")
    total_members: int = Field(description="Total member count")


class UserSquadResponse(BaseAPIModel):
    """Response model for user's current squad."""
    
    user_id: str = Field(description="Discord user ID")
    guild_id: str = Field(description="Discord guild ID")
    squad: Optional[SquadResponse] = Field(None, description="Current squad or None")
    membership: Optional[SquadMembershipResponse] = Field(None, description="Membership details")


# ============================================================================
# Error Response Schemas
# ============================================================================

class ErrorDetail(BaseAPIModel):
    """Individual error detail."""
    
    code: str = Field(description="Error code")
    message: str = Field(description="Human readable error message")
    field: Optional[str] = Field(None, description="Field that caused error")


class ErrorResponse(BaseAPIModel):
    """Standard error response format."""
    
    detail: str = Field(description="Main error message")
    type: str = Field(description="Error type")
    errors: Optional[List[ErrorDetail]] = Field(None, description="Detailed error list")
    timestamp: datetime = Field(description="Error timestamp")
    request_id: Optional[str] = Field(None, description="Request ID for tracking")
    
    @field_serializer('timestamp')
    def serialize_timestamp(self, value: datetime) -> str:
        return value.isoformat()


class ValidationErrorResponse(ErrorResponse):
    """Validation error response format."""
    
    type: str = Field(default="validation_error", description="Error type")
    errors: List[ErrorDetail] = Field(description="Validation error details")


# ============================================================================
# Utility Response Schemas
# ============================================================================

class SuccessResponse(BaseAPIModel):
    """Generic success response."""
    
    success: bool = Field(default=True, description="Operation success status")
    message: str = Field(description="Success message")
    timestamp: datetime = Field(description="Response timestamp")
    
    @field_serializer('timestamp')
    def serialize_timestamp(self, value: datetime) -> str:
        return value.isoformat()


class HealthResponse(BaseAPIModel):
    """Health check response."""
    
    status: str = Field(description="Service health status")
    version: str = Field(description="API version")
    timestamp: datetime = Field(description="Health check timestamp")
    database: bool = Field(description="Database connection status")
    redis: bool = Field(description="Redis connection status")
    
    @field_serializer('timestamp')
    def serialize_timestamp(self, value: datetime) -> str:
        return value.isoformat()