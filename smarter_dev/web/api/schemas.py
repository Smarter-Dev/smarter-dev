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
    username: Optional[str] = Field(None, description="Discord username for transaction records")
    
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
    transfer_cooldown_hours: int = Field(ge=0, description="Hours between transfers")
    streak_bonuses: Dict[int, int] = Field(description="Streak day to multiplier mapping")
    role_rewards: Dict[str, int] = Field(description="Role ID to minimum reward mapping")
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
    welcome_message: Optional[str] = Field(description="Custom welcome message")
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
    welcome_message: Optional[str] = Field(None, max_length=500, description="Custom welcome message")
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
    welcome_message: Optional[str] = Field(None, max_length=500, description="Custom welcome message")
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
    username: Optional[str] = Field(None, description="Discord username for transaction records")
    
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


# ============================================================================
# Admin Management Schemas
# ============================================================================

class APIKeyCreate(BaseAPIModel):
    """Request model for creating a new API key."""
    
    name: str = Field(..., min_length=1, max_length=255, description="Human-readable name for the API key")
    description: Optional[str] = Field(None, max_length=1000, description="Optional description of the key's purpose")
    scopes: List[str] = Field(..., min_length=1, max_length=20, description="List of permission scopes for this key")
    rate_limit_per_hour: int = Field(default=1000, ge=1, le=100000, description="Rate limit per hour for this key")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration date for the key")
    
    @field_validator('name')
    def validate_name(cls, v):
        """Validate API key name."""
        if not v or not v.strip():
            raise ValueError("API key name cannot be empty or whitespace only")
        
        # Trim whitespace
        v = v.strip()
        
        # Check length after trimming
        if len(v) < 1 or len(v) > 255:
            raise ValueError("API key name must be between 1 and 255 characters")
            
        return v
    
    @field_validator('scopes')
    def validate_scopes(cls, v):
        """Validate that all scopes are allowed."""
        if not v or len(v) == 0:
            raise ValueError("At least one scope must be provided")
            
        allowed_scopes = {
            "bot:read", "bot:write", "bot:manage",
            "admin:read", "admin:write", "admin:manage",
            "system:read", "system:write", "system:manage"
        }
        
        for scope in v:
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError("All scopes must be non-empty strings")
            if scope not in allowed_scopes:
                raise ValueError(f"Invalid scope: {scope}. Allowed scopes: {sorted(allowed_scopes)}")
        
        return v
    
    @field_validator('rate_limit_per_hour')
    def validate_rate_limit(cls, v):
        """Validate rate limit value."""
        if v < 1:
            raise ValueError("Rate limit must be at least 1 request per hour")
        if v > 100000:
            raise ValueError("Rate limit cannot exceed 100,000 requests per hour")
        return v


class APIKeyUpdate(BaseAPIModel):
    """Request model for updating an existing API key."""
    
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="Human-readable name for the API key")
    description: Optional[str] = Field(None, max_length=500, description="Optional description of the key's purpose")
    scopes: Optional[List[str]] = Field(None, min_items=1, description="List of permission scopes for this key")
    rate_limit_per_hour: Optional[int] = Field(None, ge=1, le=10000, description="Rate limit per hour for this key")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration date for the key")
    
    @field_validator('scopes')
    def validate_scopes(cls, v):
        """Validate that all scopes are allowed."""
        if v is None:
            return v
        
        allowed_scopes = {
            "bot:read", "bot:write", "bot:manage",
            "admin:read", "admin:write", "admin:manage",
            "system:read", "system:write", "system:manage"
        }
        
        for scope in v:
            if scope not in allowed_scopes:
                raise ValueError(f"Invalid scope: {scope}. Allowed scopes: {allowed_scopes}")
        
        return v
    
    @field_validator('name')
    def validate_name(cls, v):
        """Validate API key name."""
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Name cannot be empty")
        return v


class APIKeyResponse(BaseAPIModel):
    """Response model for API key operations (safe, no sensitive data)."""
    
    id: UUID = Field(..., description="Unique identifier for the API key")
    name: str = Field(..., description="Human-readable name for the API key")
    description: Optional[str] = Field(None, description="Description of the key's purpose")
    key_prefix: str = Field(..., description="First 12 characters of the API key for identification")
    scopes: List[str] = Field(..., description="List of permission scopes for this key")
    rate_limit_per_hour: int = Field(..., description="Rate limit per hour for this key")
    is_active: bool = Field(..., description="Whether the key is currently active")
    usage_count: int = Field(..., description="Number of times this key has been used")
    created_at: datetime = Field(..., description="When the key was created")
    created_by: str = Field(..., description="Who created the key")
    last_used_at: Optional[datetime] = Field(None, description="When the key was last used")
    expires_at: Optional[datetime] = Field(None, description="When the key expires (if set)")
    revoked_at: Optional[datetime] = Field(None, description="When the key was revoked (if revoked)")


class APIKeyCreateResponse(APIKeyResponse):
    """Response model for API key creation (includes the actual key - only shown once)."""
    
    api_key: str = Field(..., description="The actual API key (only shown once upon creation)")


class APIKeyListResponse(BaseAPIModel):
    """Response model for paginated API key listings."""
    
    items: List[APIKeyResponse] = Field(..., description="List of API keys")
    total: int = Field(..., description="Total number of API keys")
    page: int = Field(..., description="Current page number")
    size: int = Field(..., description="Number of items per page")
    pages: int = Field(..., description="Total number of pages")


class APIKeyRevokeResponse(BaseAPIModel):
    """Response model for API key revocation."""
    
    message: str = Field(..., description="Success message")
    key_id: str = Field(..., description="ID of the revoked key")
    revoked_at: datetime = Field(..., description="When the key was revoked")


class AdminStatsResponse(BaseAPIModel):
    """Response model for admin dashboard statistics."""
    
    total_api_keys: int = Field(..., description="Total number of API keys")
    active_api_keys: int = Field(..., description="Number of active API keys")
    revoked_api_keys: int = Field(..., description="Number of revoked API keys")
    expired_api_keys: int = Field(..., description="Number of expired API keys")
    total_api_requests: int = Field(..., description="Total API requests made")
    api_requests_today: int = Field(..., description="API requests made today")
    top_api_consumers: List[dict] = Field(..., description="Top API key consumers by usage")