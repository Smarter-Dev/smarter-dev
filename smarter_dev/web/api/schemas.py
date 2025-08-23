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
    squad_assignment: Optional[SquadResponse] = Field(None, description="Squad assigned during this claim (if auto-assigned)")
    
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
    announcement_channel: Optional[str] = Field(description="Discord channel ID for squad announcements")
    max_members: Optional[int] = Field(description="Maximum member limit")
    switch_cost: int = Field(description="Original cost to join squad")
    member_count: int = Field(description="Current member count")
    is_active: bool = Field(description="Whether squad is active")
    is_default: bool = Field(description="Whether this is the default squad for auto-assignment")
    created_at: datetime = Field(description="Squad creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")
    
    # Sale information fields (populated dynamically)
    join_cost_info: Optional[SquadCostInfo] = Field(None, description="Cost information for joining this squad")
    switch_cost_info: Optional[SquadCostInfo] = Field(None, description="Cost information for switching to this squad")
    
    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class SquadCreate(BaseAPIModel):
    """Request model for creating a squad."""
    
    role_id: str = Field(description="Discord role ID for squad")
    name: str = Field(min_length=1, max_length=100, description="Squad name")
    description: Optional[str] = Field(None, max_length=500, description="Squad description")
    welcome_message: Optional[str] = Field(None, max_length=500, description="Custom welcome message")
    announcement_channel: Optional[str] = Field(None, max_length=255, description="Discord channel ID for squad announcements")
    max_members: Optional[int] = Field(None, ge=1, le=1000, description="Member limit")
    switch_cost: int = Field(ge=0, le=10000, default=50, description="Cost to join")
    is_default: bool = Field(default=False, description="Whether this is the default squad for auto-assignment")
    
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
    announcement_channel: Optional[str] = Field(None, max_length=255, description="Discord channel ID for squad announcements")
    max_members: Optional[int] = Field(None, ge=1, le=1000, description="Member limit")
    switch_cost: Optional[int] = Field(None, ge=0, le=10000, description="Join cost")
    is_active: Optional[bool] = Field(None, description="Active status")
    is_default: Optional[bool] = Field(None, description="Whether this is the default squad for auto-assignment")


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
    username: Optional[str] = Field(None, max_length=100, description="Discord username for transaction records")
    
    @field_validator('user_id')
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """Validate Discord user ID."""
        if not v or not v.strip():
            raise ValueError("User ID cannot be empty")
        
        v = v.strip()
        
        try:
            user_id_int = int(v)
            if user_id_int <= 0:
                raise ValueError("User ID must be positive")
            # Discord IDs should be reasonable length (snowflakes are typically 17-19 digits)
            if len(v) < 10 or len(v) > 20:
                raise ValueError("User ID length is invalid for Discord snowflake")
            return v
        except ValueError as e:
            if "invalid literal" in str(e).lower():
                raise ValueError("User ID must contain only digits")
            raise e
    
    @field_validator('username')
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        """Validate Discord username."""
        if v is None:
            return v
        
        # Strip whitespace
        v = v.strip()
        if not v:
            return None
        
        # Discord usernames can contain various characters, be permissive
        if len(v) > 100:
            raise ValueError("Username is too long (max 100 characters)")
        
        return v


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


# ============================================================================
# Help Conversation Schemas
# ============================================================================

class HelpConversationCreate(BaseAPIModel):
    """Request model for creating a help conversation record."""
    
    session_id: str = Field(..., description="Session identifier for linking conversations")
    guild_id: str = Field(..., description="Discord guild ID")
    channel_id: str = Field(..., description="Discord channel ID") 
    user_id: str = Field(..., description="Discord user ID")
    user_username: str = Field(..., description="Username at time of conversation")
    interaction_type: str = Field(..., description="Type of interaction: 'slash_command' or 'mention'")
    context_messages: Optional[List[dict]] = Field(None, description="Sanitized context messages")
    user_question: str = Field(..., description="User's question or request")
    bot_response: str = Field(..., description="Bot's generated response")
    tokens_used: int = Field(..., description="AI tokens consumed")
    response_time_ms: Optional[int] = Field(None, description="Response generation time in milliseconds")
    retention_policy: str = Field("standard", description="Data retention policy")
    is_sensitive: bool = Field(False, description="Whether conversation contains sensitive information")
    command_metadata: Optional[dict] = Field(None, description="Command-specific metadata for analytics")


class HelpConversationResponse(BaseAPIModel):
    """Response model for help conversation data."""
    
    id: UUID = Field(..., description="Unique conversation identifier")
    session_id: str = Field(..., description="Session identifier")
    guild_id: str = Field(..., description="Discord guild ID")
    channel_id: str = Field(..., description="Discord channel ID")
    user_id: str = Field(..., description="Discord user ID")
    user_username: str = Field(..., description="Username at time of conversation")
    started_at: datetime = Field(..., description="When conversation started")
    last_activity_at: datetime = Field(..., description="Most recent activity")
    interaction_type: str = Field(..., description="Type of interaction")
    is_resolved: bool = Field(..., description="Whether conversation was resolved")
    context_messages: Optional[List[dict]] = Field(None, description="Context messages")
    user_question: str = Field(..., description="User's question")
    bot_response: str = Field(..., description="Bot's response")
    tokens_used: int = Field(..., description="Tokens consumed")
    response_time_ms: Optional[int] = Field(None, description="Response time in ms")
    retention_policy: str = Field(..., description="Retention policy")
    expires_at: Optional[datetime] = Field(None, description="Expiration timestamp")
    is_sensitive: bool = Field(..., description="Contains sensitive information")
    command_metadata: Optional[dict] = Field(None, description="Command-specific metadata for analytics")
    created_at: datetime = Field(..., description="Record creation time")
    updated_at: Optional[datetime] = Field(None, description="Last update time")


class HelpConversationListResponse(BaseAPIModel):
    """Response model for paginated conversation listings."""
    
    items: List[HelpConversationResponse] = Field(..., description="List of conversations")
    total: int = Field(..., description="Total number of conversations")
    page: int = Field(..., description="Current page number")
    size: int = Field(..., description="Number of items per page")
    pages: int = Field(..., description="Total number of pages")


class HelpConversationCreateResponse(BaseAPIModel):
    """Response model for conversation creation."""
    
    id: UUID = Field(..., description="Created conversation ID")
    message: str = Field(..., description="Success message")
    created_at: datetime = Field(..., description="Creation timestamp")


class HelpConversationStatsResponse(BaseAPIModel):
    """Response model for help conversation statistics."""
    
    total_conversations: int = Field(..., description="Total conversations")
    conversations_today: int = Field(..., description="Conversations today")
    total_tokens_used: int = Field(..., description="Total tokens consumed")
    tokens_used_today: int = Field(..., description="Tokens used today")
    average_response_time_ms: Optional[int] = Field(None, description="Average response time")
    top_users: List[dict] = Field(..., description="Most active users")
    conversation_types: dict = Field(..., description="Breakdown by interaction type")
    resolution_rate: float = Field(..., description="Percentage of resolved conversations")


# ============================================================================
# Squad Sale Event Schemas
# ============================================================================

class SquadSaleEventCreate(BaseAPIModel):
    """Request model for creating a squad sale event."""
    
    name: str = Field(min_length=1, max_length=100, description="Name of the sale event")
    description: str = Field(max_length=500, description="Description of the sale event")
    start_time: datetime = Field(description="When the sale event starts (UTC)")
    duration_hours: int = Field(ge=1, le=720, description="Duration of the sale event in hours (1-720)")
    join_discount_percent: int = Field(ge=0, le=100, description="Percentage discount for joining squads (0-100)")
    switch_discount_percent: int = Field(ge=0, le=100, description="Percentage discount for switching squads (0-100)")
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate sale event name."""
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty")
        return v
    
    @field_validator('description')
    @classmethod
    def validate_description(cls, v: str) -> str:
        """Validate sale event description."""
        return v.strip()
    
    @field_validator('start_time')
    @classmethod
    def validate_start_time(cls, v: datetime) -> datetime:
        """Validate start time is in the future."""
        from datetime import timezone
        now = datetime.now(timezone.utc)
        if v <= now:
            raise ValueError("Start time must be in the future")
        return v


class SquadSaleEventUpdate(BaseAPIModel):
    """Request model for updating a squad sale event."""
    
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="Name of the sale event")
    description: Optional[str] = Field(None, max_length=500, description="Description of the sale event") 
    start_time: Optional[datetime] = Field(None, description="When the sale event starts (UTC)")
    duration_hours: Optional[int] = Field(None, ge=1, le=720, description="Duration of the sale event in hours")
    join_discount_percent: Optional[int] = Field(None, ge=0, le=100, description="Percentage discount for joining squads")
    switch_discount_percent: Optional[int] = Field(None, ge=0, le=100, description="Percentage discount for switching squads")
    is_active: Optional[bool] = Field(None, description="Whether the sale event is active")
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        """Validate sale event name."""
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Name cannot be empty")
        return v
    
    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        """Validate sale event description."""
        return v.strip() if v is not None else v


class SquadSaleEventResponse(BaseAPIModel):
    """Response model for squad sale event."""
    
    id: UUID = Field(description="Unique identifier for the sale event")
    guild_id: str = Field(description="Discord guild ID")
    name: str = Field(description="Name of the sale event")
    description: str = Field(description="Description of the sale event")
    start_time: datetime = Field(description="When the sale event starts")
    duration_hours: int = Field(description="Duration of the sale event in hours")
    join_discount_percent: int = Field(description="Percentage discount for joining squads")
    switch_discount_percent: int = Field(description="Percentage discount for switching squads")
    is_active: bool = Field(description="Whether this sale event is active")
    created_by: str = Field(description="Who created this event")
    created_at: datetime = Field(description="When the event was created")
    updated_at: datetime = Field(description="When the event was last updated")
    
    # Computed fields
    end_time: Optional[datetime] = Field(None, description="When the sale event ends")
    is_currently_active: Optional[bool] = Field(None, description="Whether the event is currently active")
    has_started: Optional[bool] = Field(None, description="Whether the event has started")
    has_ended: Optional[bool] = Field(None, description="Whether the event has ended")
    time_remaining_hours: Optional[int] = Field(None, description="Hours remaining in the sale")
    days_until_start: Optional[int] = Field(None, description="Days until sale starts")
    
    @field_serializer('start_time', 'created_at', 'updated_at', 'end_time')
    def serialize_datetime(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class SquadSaleEventListResponse(BaseAPIModel):
    """Response model for paginated sale event listings."""
    
    items: List[SquadSaleEventResponse] = Field(description="List of sale events")
    total: int = Field(description="Total number of sale events")
    page: int = Field(description="Current page number")
    size: int = Field(description="Number of items per page")
    pages: int = Field(description="Total number of pages")


class SquadSaleEventCreateResponse(BaseAPIModel):
    """Response model for sale event creation."""
    
    id: UUID = Field(description="Created sale event ID")
    message: str = Field(description="Success message")
    created_at: datetime = Field(description="Creation timestamp")
    
    @field_serializer('created_at')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class ActiveSaleEventResponse(BaseAPIModel):
    """Response model for active sale events affecting costs."""
    
    event_name: str = Field(description="Name of the active sale event")
    event_id: UUID = Field(description="ID of the active sale event")
    join_discount_percent: int = Field(description="Discount percentage for joining")
    switch_discount_percent: int = Field(description="Discount percentage for switching")
    time_remaining_hours: Optional[int] = Field(None, description="Hours remaining in sale")
    end_time: datetime = Field(description="When the sale ends")
    
    @field_serializer('end_time')
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class SquadCostInfo(BaseAPIModel):
    """Cost information including sale discounts."""
    
    original_cost: int = Field(description="Original cost before any discounts")
    current_cost: int = Field(description="Current cost after applying best available discount")
    discount_percent: Optional[int] = Field(None, description="Discount percentage applied (0-100)")
    active_sale: Optional[ActiveSaleEventResponse] = Field(None, description="Active sale event providing discount")
    is_on_sale: bool = Field(description="Whether this action is currently discounted")