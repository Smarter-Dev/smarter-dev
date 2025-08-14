"""Database models for the Smarter Dev application."""

from __future__ import annotations

from datetime import datetime, timezone, date
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime, Date
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index, UniqueConstraint, CheckConstraint
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from smarter_dev.shared.database import Base


class BytesBalance(Base):
    """User balance tracking for the bytes economy system.
    
    Tracks user balances, transaction totals, and daily streak information
    per guild. Uses compound primary key (guild_id, user_id) to ensure
    one balance record per user per guild.
    """
    
    __tablename__ = "bytes_balances"
    
    # Compound primary key
    guild_id: Mapped[str] = mapped_column(
        String, 
        primary_key=True,
        doc="Discord guild (server) snowflake ID"
    )
    user_id: Mapped[str] = mapped_column(
        String, 
        primary_key=True,
        doc="Discord user snowflake ID"
    )
    
    # Balance and transaction tracking
    balance: Mapped[int] = mapped_column(
        BigInteger, 
        nullable=False, 
        default=0,
        doc="Current balance of bytes"
    )
    total_received: Mapped[int] = mapped_column(
        BigInteger, 
        nullable=False, 
        default=0,
        doc="Total bytes received from all sources"
    )
    total_sent: Mapped[int] = mapped_column(
        BigInteger, 
        nullable=False, 
        default=0,
        doc="Total bytes sent to other users"
    )
    
    # Daily reward streak tracking
    streak_count: Mapped[int] = mapped_column(
        BigInteger, 
        nullable=False, 
        default=0,
        doc="Current consecutive daily reward streak"
    )
    last_daily: Mapped[Optional[date]] = mapped_column(
        Date, 
        nullable=True,
        doc="Date of last daily reward claim"
    )
    
    def __init__(self, **kwargs):
        """Initialize BytesBalance with default values."""
        # Set defaults for fields not provided
        kwargs.setdefault('balance', 0)
        kwargs.setdefault('total_received', 0)
        kwargs.setdefault('total_sent', 0)
        kwargs.setdefault('streak_count', 0)
        super().__init__(**kwargs)


class BytesTransaction(Base):
    """Transaction record for the bytes economy system.
    
    Records all bytes transfers between users, including peer-to-peer
    transfers and system rewards. Uses UUID primary key for uniqueness
    and includes indexes for common query patterns.
    """
    
    __tablename__ = "bytes_transactions"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique transaction identifier"
    )
    
    # Guild context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord guild (server) snowflake ID"
    )
    
    # Transaction participants
    giver_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord user ID of the giver"
    )
    giver_username: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Username of the giver at time of transaction"
    )
    receiver_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord user ID of the receiver"
    )
    receiver_username: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Username of the receiver at time of transaction"
    )
    
    # Transaction details
    amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        doc="Amount of bytes transferred"
    )
    reason: Mapped[Optional[str]] = mapped_column(
        String(200),  # Max 200 chars as per specification
        nullable=True,
        doc="Optional reason for the transaction"
    )
    
    # Timestamp fields
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the transaction was created"
    )
    
    # Indexes for common queries - as per specification
    __table_args__ = (
        Index("ix_bytes_transactions_guild_id", "guild_id"),
        Index("ix_bytes_transactions_created_at", "created_at"),  # Missing from specification
        Index("ix_bytes_transactions_giver_id", "giver_id"),  
        Index("ix_bytes_transactions_receiver_id", "receiver_id"),
        Index("ix_bytes_transactions_guild_giver", "guild_id", "giver_id"),
        Index("ix_bytes_transactions_guild_receiver", "guild_id", "receiver_id"),
    )
    
    def __init__(self, **kwargs):
        """Initialize BytesTransaction with auto-generated ID if not provided."""
        # Set default UUID if not provided
        kwargs.setdefault('id', uuid4())
        super().__init__(**kwargs)


class BytesConfig(Base):
    """Configuration settings for the bytes economy system per guild.
    
    Stores guild-specific settings for daily rewards, transfer limits,
    streak bonuses, and role-based rewards. One config record per guild.
    Matches Session 2 specification exactly.
    """
    
    __tablename__ = "bytes_configs"
    
    # Primary key
    guild_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord guild (server) snowflake ID"
    )
    
    # Balance and reward settings
    starting_balance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        doc="Initial balance for new users"
    )
    daily_amount: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10,
        doc="Base amount of bytes given for daily rewards"
    )
    
    # Streak bonus configuration (JSON)
    streak_bonuses: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {7: 2, 14: 4, 30: 10, 60: 20},
        doc="Streak day thresholds to bonus multipliers mapping"
    )
    
    # Transfer settings
    max_transfer: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1000,
        doc="Maximum amount that can be transferred at once"
    )
    transfer_cooldown_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Hours between transfers (0 = no cooldown)"
    )
    
    # Role-based rewards (JSON)
    role_rewards: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="Role ID to minimum received amount mapping"
    )
    
    def __init__(self, **kwargs):
        """Initialize BytesConfig with default values per specification."""
        # Set defaults for fields not provided
        kwargs.setdefault('starting_balance', 100)
        kwargs.setdefault('daily_amount', 10)
        kwargs.setdefault('streak_bonuses', {8: 2, 16: 4, 32: 8, 64: 16})
        kwargs.setdefault('max_transfer', 1000)
        kwargs.setdefault('transfer_cooldown_hours', 0)
        kwargs.setdefault('role_rewards', {})
        super().__init__(**kwargs)
    
    @classmethod
    def get_defaults(cls, guild_id: str) -> 'BytesConfig':
        """Get default configuration for a guild.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            BytesConfig instance with default values
        """
        return cls(
            guild_id=guild_id,
            starting_balance=100,
            daily_amount=10,
            streak_bonuses={8: 2, 16: 4, 32: 8, 64: 16},
            max_transfer=1000,
            transfer_cooldown_hours=0,
            role_rewards={}
        )


class Squad(Base):
    """Squad definition for team-based groupings.
    
    Represents a team that users can join, with configurable costs
    and member limits. Connected to Discord roles for permissions.
    """
    
    __tablename__ = "squads"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique squad identifier"
    )
    
    # Guild and role linkage
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord guild (server) snowflake ID"
    )
    role_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord role snowflake ID associated with this squad"
    )
    
    # Squad metadata
    name: Mapped[str] = mapped_column(
        String(100),  # Max 100 chars as per specification
        nullable=False,
        doc="Display name of the squad"
    )
    description: Mapped[Optional[str]] = mapped_column(
        String(500),  # Max 500 chars as per specification
        nullable=True,
        doc="Optional description of the squad"
    )
    
    # Squad settings
    switch_cost: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=50,  # Default 50 as per specification
        doc="Cost in bytes to switch to this squad"
    )
    max_members: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Maximum number of members (null = no limit)"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="Whether this squad is active and accepting members"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Whether this is the default squad for auto-assignment when users earn bytes"
    )
    welcome_message: Mapped[Optional[str]] = mapped_column(
        String(500),  # Max 500 chars for welcome message
        nullable=True,
        doc="Custom welcome message shown when users join this squad"
    )
    
    # Indexes and constraints for common queries
    __table_args__ = (
        Index("ix_squads_guild_id", "guild_id"),
        Index("ix_squads_role_id", "role_id"),
        Index("ix_squads_guild_active", "guild_id", "is_active"),
        Index("ix_squads_guild_default", "guild_id", "is_default"),
        UniqueConstraint("guild_id", "role_id", name="uq_squads_guild_role"),  # Unique per specification
        # Note: The unique constraint for default squads is handled in the migration
        # because SQLAlchemy doesn't support partial unique constraints directly
    )
    
    def __init__(self, **kwargs):
        """Initialize Squad with auto-generated ID and defaults."""
        kwargs.setdefault('id', uuid4())
        kwargs.setdefault('switch_cost', 50)
        kwargs.setdefault('is_active', True)
        kwargs.setdefault('is_default', False)
        kwargs.setdefault('welcome_message', "Welcome to the squad! We're glad to have you aboard.")
        super().__init__(**kwargs)


class SquadMembership(Base):
    """Membership relationship between users and squads.
    
    Tracks which users belong to which squads. Uses compound primary key
    (squad_id, user_id) which is an improvement over the original spec's
    (guild_id, user_id) as it better enforces the business constraint.
    """
    
    __tablename__ = "squad_memberships"
    
    # Compound primary key (improved design)
    squad_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("squads.id", ondelete="CASCADE"),
        primary_key=True,
        doc="UUID of the squad"
    )
    user_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord user snowflake ID"
    )
    
    # Additional fields per specification
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord guild (server) snowflake ID for indexing and queries"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        doc="Timestamp when the user joined this squad"
    )
    
    # Indexes for common queries
    __table_args__ = (
        Index("ix_squad_memberships_squad_id", "squad_id"),
        Index("ix_squad_memberships_user_id", "user_id"),
        Index("ix_squad_memberships_guild_id", "guild_id"),
        Index("ix_squad_memberships_guild_user", "guild_id", "user_id"),
    )
    
    def __init__(self, **kwargs):
        """Initialize SquadMembership with default joined_at timestamp."""
        kwargs.setdefault('joined_at', datetime.now(timezone.utc))
        super().__init__(**kwargs)


class APIKey(Base):
    """API key model for secure authentication and access control.
    
    Stores cryptographically secure API keys with hashing, scoping,
    and usage tracking capabilities. Designed for enterprise-grade
    API authentication with proper security practices.
    """
    
    __tablename__ = "api_keys"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the API key"
    )
    
    # Key identification and naming
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Human-readable name for the API key (e.g., 'Discord Bot', 'Admin Dashboard')"
    )
    description: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc="Optional description of the API key's purpose and usage"
    )
    
    # Secure key storage
    key_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        doc="SHA-256 hash of the API key for secure storage"
    )
    key_prefix: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        doc="First 12 characters of the key for display (e.g., 'sk-abc123de')"
    )
    
    # Access control
    scopes: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of permission scopes (e.g., ['bytes:read', 'squads:write'])"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="Whether the API key is active and can be used"
    )
    
    # Expiration and lifecycle
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Optional expiration timestamp for the API key"
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Timestamp when the API key was revoked (if revoked)"
    )
    
    # Usage tracking
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Timestamp of the last successful API request using this key"
    )
    usage_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Total number of successful API requests using this key"
    )
    
    # Multi-tier rate limiting
    rate_limit_per_second: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10,
        doc="Maximum number of requests allowed per second for this key"
    )
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=180,
        doc="Maximum number of requests allowed per minute for this key"
    )
    rate_limit_per_15_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2500,
        doc="Maximum number of requests allowed per 15 minutes for this key"
    )
    
    # Legacy rate limiting (kept for backward compatibility)
    rate_limit_per_hour: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10000,
        doc="Maximum number of requests allowed per hour for this key (legacy)"
    )
    
    # Audit trail
    created_by: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username or identifier of who created this API key"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the API key was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the API key was last modified"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_api_keys_hash", "key_hash"),
        Index("ix_api_keys_active", "is_active", postgresql_where="is_active = true"),
        Index("ix_api_keys_prefix", "key_prefix"),
        Index("ix_api_keys_created_by", "created_by"),
        UniqueConstraint("key_hash", name="uq_api_keys_hash"),
    )
    
    def __init__(self, **kwargs):
        """Initialize APIKey with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        super().__init__(**kwargs)
    
    @property
    def is_expired(self) -> bool:
        """Check if the API key has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at
    
    @property
    def is_valid(self) -> bool:
        """Check if the API key is valid (active and not expired)."""
        return self.is_active and not self.is_expired
    
    def __repr__(self) -> str:
        """String representation of the API key."""
        status = "active" if self.is_valid else "inactive"
        return f"<APIKey(name='{self.name}', prefix='{self.key_prefix}', status='{status}')>"


class SecurityLog(Base):
    """Security log entries for audit trail and monitoring.
    
    Records all security-related events including API key usage,
    authentication attempts, rate limiting events, and administrative
    operations. Provides comprehensive audit trail for compliance
    and security monitoring.
    """
    
    __tablename__ = "security_logs"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the log entry"
    )
    
    # Event classification
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        doc="Type of action performed (e.g., 'api_key_created', 'authentication_failed')"
    )
    
    # Related entities
    api_key_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="API key involved in the action (if applicable)"
    )
    user_identifier: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        doc="User identifier (Discord ID, admin username, etc.)"
    )
    
    # Request context
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),  # IPv6 max length
        nullable=True,
        index=True,
        doc="Client IP address"
    )
    user_agent: Mapped[Optional[str]] = mapped_column(
        String(512),
        nullable=True,
        doc="Client user agent string"
    )
    request_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
        doc="Request correlation ID"
    )
    
    # Event outcome and details
    success: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        index=True,
        doc="Whether the action was successful"
    )
    details: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Detailed description of the event"
    )
    event_metadata: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        doc="Additional structured metadata"
    )
    
    # Timestamps
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        doc="When the event occurred"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_security_logs_action_timestamp", "action", "timestamp"),
        Index("ix_security_logs_success_timestamp", "success", "timestamp"),
        Index("ix_security_logs_api_key_timestamp", "api_key_id", "timestamp"),
        Index("ix_security_logs_user_timestamp", "user_identifier", "timestamp"),
        Index("ix_security_logs_ip_timestamp", "ip_address", "timestamp"),
    )
    
    def __init__(self, **kwargs):
        """Initialize SecurityLog with default timestamp."""
        kwargs.setdefault('timestamp', datetime.now(timezone.utc))
        super().__init__(**kwargs)
    
    def __repr__(self) -> str:
        """String representation of the security log."""
        status = "SUCCESS" if self.success else "FAILURE"
        return f"<SecurityLog(action='{self.action}', status='{status}', timestamp='{self.timestamp}')>"


class HelpConversation(Base):
    """Help agent conversation tracking for audit and analytics.
    
    Stores complete conversation data including user questions, bot responses,
    context messages, and performance metrics. Designed for admin auditing,
    analytics, and system improvement with privacy controls and data retention.
    """
    
    __tablename__ = "help_conversations"
    
    # Primary identification
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the conversation"
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        doc="Session identifier for linking related conversations"
    )
    
    # Discord context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord guild (server) snowflake ID"
    )
    channel_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord channel snowflake ID"
    )
    user_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord user snowflake ID"
    )
    user_username: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username at time of conversation"
    )
    
    # Conversation metadata
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        doc="When the conversation started"
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        doc="Most recent activity timestamp"
    )
    interaction_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="Type of interaction: 'slash_command' or 'mention'"
    )
    is_resolved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether the conversation was resolved successfully"
    )
    
    # Content (with privacy controls)
    context_messages: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        doc="Sanitized context messages from channel history"
    )
    user_question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="User's question or request"
    )
    bot_response: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Bot's generated response"
    )
    
    # Performance tracking
    tokens_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="AI tokens consumed for response generation"
    )
    response_time_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Response generation time in milliseconds"
    )
    
    # Privacy and retention
    retention_policy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="standard",
        doc="Data retention policy: 'standard', 'minimal', 'sensitive'"
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="Auto-deletion timestamp based on retention policy"
    )
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether conversation contains sensitive information"
    )
    
    # Analytics metadata
    command_metadata: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        doc="Command-specific metadata for analytics and tracking"
    )
    
    # Audit fields
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="When the record was created"
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
        doc="When the record was last updated"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_help_conversations_guild_started", "guild_id", "started_at"),
        Index("ix_help_conversations_user_started", "user_id", "started_at"),
        Index("ix_help_conversations_session_started", "session_id", "started_at"),
        Index("ix_help_conversations_expires_at", "expires_at"),
        Index("ix_help_conversations_tokens_started", "tokens_used", "started_at"),
    )
    
    def __init__(self, **kwargs):
        """Initialize HelpConversation with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('started_at', now)
        kwargs.setdefault('last_activity_at', now)
        kwargs.setdefault('created_at', now)
        
        # Set default expiration based on retention policy
        if 'retention_policy' in kwargs and 'expires_at' not in kwargs:
            retention = kwargs['retention_policy']
            if retention == "sensitive":
                from datetime import timedelta
                kwargs['expires_at'] = now + timedelta(days=7)
            elif retention == "minimal":
                from datetime import timedelta
                kwargs['expires_at'] = now + timedelta(days=30)
            elif retention == "standard":
                from datetime import timedelta
                kwargs['expires_at'] = now + timedelta(days=90)
        
        super().__init__(**kwargs)
    
    def __repr__(self) -> str:
        """String representation of the help conversation."""
        return f"<HelpConversation(id='{self.id}', user='{self.user_username}', tokens={self.tokens_used})>"


class BlogPost(Base):
    """Blog post model for the website blog feature.
    
    Stores blog posts with markdown content, slug-based URLs, and publishing status.
    Supports draft and published states with optional publishing timestamps.
    """
    
    __tablename__ = "blog_posts"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the blog post"
    )
    
    # Content fields
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="Blog post title"
    )
    slug: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        unique=True,
        index=True,
        doc="URL-friendly slug for the blog post"
    )
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Blog post content in Markdown format"
    )
    author: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Author name or identifier"
    )
    
    # Publishing status
    is_published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether the blog post is published"
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="Timestamp when the blog post was published"
    )
    
    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the blog post was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the blog post was last updated"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_blog_posts_published", "is_published", "published_at"),
        Index("ix_blog_posts_author", "author"),
        Index("ix_blog_posts_created_at", "created_at"),
        UniqueConstraint("slug", name="uq_blog_posts_slug"),
    )
    
    def __init__(self, **kwargs):
        """Initialize BlogPost with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        super().__init__(**kwargs)
    
    @property
    def excerpt(self) -> str:
        """Get a brief excerpt from the blog post body."""
        # Simple excerpt: first 200 characters, cut at word boundary
        if len(self.body) <= 200:
            return self.body
        
        excerpt = self.body[:200]
        # Find the last space to avoid cutting words
        last_space = excerpt.rfind(' ')
        if last_space > 100:  # Don't make excerpt too short
            excerpt = excerpt[:last_space]
        
        return excerpt + "..."
    
    def __repr__(self) -> str:
        """String representation of the blog post."""
        status = "published" if self.is_published else "draft"
        return f"<BlogPost(title='{self.title}', slug='{self.slug}', status='{status}')>"


class ForumAgent(Base):
    """Forum monitoring agent configuration for AI-driven post responses.
    
    Stores per-guild agent configurations that monitor forum channels and
    automatically evaluate new posts to determine if they warrant AI responses.
    Each agent has a customizable system prompt and configurable thresholds.
    """
    
    __tablename__ = "forum_agents"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the forum agent"
    )
    
    # Guild context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord guild (server) snowflake ID"
    )
    
    # Agent identification
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Human-readable name for the agent"
    )
    description: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="",
        doc="Optional description of the agent's purpose"
    )
    
    # Agent configuration
    system_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="System prompt that defines agent behavior and response criteria"
    )
    monitored_forums: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of Discord forum channel IDs to monitor"
    )
    
    # Response settings
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this agent is actively monitoring and responding"
    )
    response_threshold: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.7,
        doc="Minimum confidence score (0.0-1.0) required to post a response"
    )
    max_responses_per_hour: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        doc="Maximum number of responses this agent can make per hour"
    )
    
    # Audit fields
    created_by: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username or identifier of who created this agent"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the agent was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the agent was last modified"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_forum_agents_guild_id", "guild_id"),
        Index("ix_forum_agents_guild_active", "guild_id", "is_active"),
        Index("ix_forum_agents_created_by", "created_by"),
        UniqueConstraint("guild_id", "name", name="uq_forum_agents_guild_name"),
        # Validation constraints
        CheckConstraint("response_threshold >= 0.0 AND response_threshold <= 1.0", name="ck_forum_agents_threshold_range"),
        CheckConstraint("max_responses_per_hour >= 0", name="ck_forum_agents_max_responses_positive"),
    )
    
    def __init__(self, **kwargs):
        """Initialize ForumAgent with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        super().__init__(**kwargs)
    
    def __repr__(self) -> str:
        """String representation of the forum agent."""
        status = "active" if self.is_active else "inactive"
        return f"<ForumAgent(name='{self.name}', guild_id='{self.guild_id}', status='{status}')>"


class ForumAgentResponse(Base):
    """Record of forum agent post evaluation and response.
    
    Tracks each time an agent evaluates a forum post, including the AI's
    decision-making process, confidence score, token usage, and whether
    a response was actually posted. Used for analytics and audit purposes.
    """
    
    __tablename__ = "forum_agent_responses"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for this response record"
    )
    
    # Agent relationship
    agent_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("forum_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Forum agent that evaluated this post"
    )
    
    # Discord context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord guild (server) snowflake ID"
    )
    channel_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord forum channel snowflake ID"
    )
    thread_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord thread (forum post) snowflake ID"
    )
    
    # Original post data
    post_title: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
        doc="Title of the forum post"
    )
    post_content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Content of the forum post"
    )
    author_display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Display name of the post author"
    )
    post_tags: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="Forum post tags as list of strings"
    )
    attachments: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of attachment filenames from the post"
    )
    
    # AI evaluation results
    decision_reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="AI's reasoning for whether to respond or not"
    )
    confidence_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="AI confidence score (0.0-1.0) for the response decision"
    )
    response_content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        doc="AI-generated response content (empty if no response)"
    )
    
    # Performance tracking
    tokens_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Total AI tokens consumed for evaluation and response generation"
    )
    response_time_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Time taken for AI evaluation and response generation in milliseconds"
    )
    
    # Response status
    responded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether a response was actually posted to Discord"
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="Timestamp when the response was posted (if responded=True)"
    )
    
    # Audit timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
        doc="Timestamp when the post was evaluated"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_forum_agent_responses_agent_created", "agent_id", "created_at"),
        Index("ix_forum_agent_responses_guild_created", "guild_id", "created_at"),
        Index("ix_forum_agent_responses_channel_created", "channel_id", "created_at"),
        Index("ix_forum_agent_responses_responded_at", "responded_at"),
        Index("ix_forum_agent_responses_tokens_created", "tokens_used", "created_at"),
        # Validation constraints
        CheckConstraint("confidence_score >= 0.0 AND confidence_score <= 1.0", name="ck_forum_agent_responses_confidence_range"),
        CheckConstraint("tokens_used >= 0", name="ck_forum_agent_responses_tokens_positive"),
        CheckConstraint("response_time_ms >= 0", name="ck_forum_agent_responses_time_positive"),
    )
    
    def __init__(self, **kwargs):
        """Initialize ForumAgentResponse with automatic timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        
        # Auto-set responded_at if responded is True
        if kwargs.get('responded', False) and 'responded_at' not in kwargs:
            kwargs['responded_at'] = now
            
        super().__init__(**kwargs)
    
    def __repr__(self) -> str:
        """String representation of the forum agent response."""
        status = "responded" if self.responded else "evaluated"
        return f"<ForumAgentResponse(agent_id='{self.agent_id}', thread_id='{self.thread_id}', status='{status}')>"


class Campaign(Base):
    """Campaign definition for challenge competitions.
    
    Represents a timed series of challenges that are released on a schedule
    with configurable announcement channels and scoring systems.
    """
    
    __tablename__ = "campaigns"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique campaign identifier"
    )
    
    # Guild context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord guild (server) snowflake ID"
    )
    
    # Campaign metadata
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="Campaign title/name"
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        doc="Detailed campaign description and rules"
    )
    
    # Campaign schedule
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        doc="When the campaign begins and first challenge is released"
    )
    release_cadence_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=24,
        doc="Hours between challenge releases (1-168)"
    )
    
    # Discord integration
    announcement_channels: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of Discord channel IDs for challenge announcements"
    )
    
    # Campaign status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this campaign is active and running"
    )
    
    # Audit fields
    created_by: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username or identifier of who created this campaign"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the campaign was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the campaign was last modified"
    )
    
    # Relationships
    challenges: Mapped[list["Challenge"]] = relationship(
        "Challenge",
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="Challenge.order_position"
    )
    scheduled_messages: Mapped[list["ScheduledMessage"]] = relationship(
        "ScheduledMessage",
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="ScheduledMessage.scheduled_time"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_campaigns_guild_id", "guild_id"),
        Index("ix_campaigns_guild_active", "guild_id", "is_active"),
        Index("ix_campaigns_start_time", "start_time"),
        Index("ix_campaigns_created_by", "created_by"),
        UniqueConstraint("guild_id", "title", name="uq_campaigns_guild_title"),
        # Validation constraints
        CheckConstraint("release_cadence_hours >= 1 AND release_cadence_hours <= 168", name="ck_campaigns_cadence_range"),
    )
    
    def __init__(self, **kwargs):
        """Initialize Campaign with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('release_cadence_hours', 24)
        kwargs.setdefault('description', '')
        kwargs.setdefault('announcement_channels', [])
        super().__init__(**kwargs)
    
    @property
    def is_started(self) -> bool:
        """Check if the campaign has started."""
        return datetime.now(timezone.utc) >= self.start_time
    
    @property
    def days_until_start(self) -> Optional[int]:
        """Get days until campaign starts (None if already started)."""
        if self.is_started:
            return None
        delta = self.start_time - datetime.now(timezone.utc)
        return delta.days
    
    def __repr__(self) -> str:
        """String representation of the campaign."""
        status = "active" if self.is_active else "inactive"
        return f"<Campaign(title='{self.title}', guild_id='{self.guild_id}', status='{status}')>"


class Challenge(Base):
    """Individual challenge within a campaign.
    
    Represents a single coding challenge with input generation and solution
    validation scripts, released according to the campaign schedule.
    """
    
    __tablename__ = "challenges"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique challenge identifier"
    )
    
    # Campaign relationship
    campaign_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Campaign this challenge belongs to"
    )
    
    # Challenge metadata
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="Challenge title/name"
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Problem statement and requirements"
    )
    
    # Challenge configuration
    order_position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        doc="Order position in campaign (1-based)"
    )
    points_value: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        doc="Base points awarded for correct solution"
    )
    
    # Challenge scripts
    python_script: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Main Python script content for the challenge"
    )
    input_generator_script: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Python script to generate personalized inputs"
    )
    solution_validator_script: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Python script to validate submitted answers"
    )
    
    # Release tracking
    is_released: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether this challenge has been released to participants"
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="Timestamp when the challenge was released"
    )
    
    # Announcement tracking
    is_announced: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether this challenge has been announced to Discord channels"
    )
    announced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="Timestamp when the challenge was announced to Discord channels"
    )
    
    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the challenge was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the challenge was last modified"
    )
    
    # Relationships
    campaign: Mapped["Campaign"] = relationship(
        "Campaign",
        back_populates="challenges"
    )
    challenge_inputs: Mapped[list["ChallengeInput"]] = relationship(
        "ChallengeInput",
        back_populates="challenge",
        cascade="all, delete-orphan"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_challenges_campaign_id", "campaign_id"),
        Index("ix_challenges_campaign_position", "campaign_id", "order_position"),
        Index("ix_challenges_is_released", "is_released"),
        Index("ix_challenges_released_at", "released_at"),
        Index("ix_challenges_is_announced", "is_announced"),
        Index("ix_challenges_announced_at", "announced_at"),
        UniqueConstraint("campaign_id", "order_position", name="uq_challenges_campaign_position"),
        # Validation constraints
        CheckConstraint("order_position >= 1", name="ck_challenges_position_positive"),
        CheckConstraint("points_value >= 0", name="ck_challenges_points_non_negative"),
    )
    
    def __init__(self, **kwargs):
        """Initialize Challenge with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('points_value', 100)
        kwargs.setdefault('is_released', False)
        kwargs.setdefault('is_announced', False)
        
        # Auto-set timestamps if flags are True
        if kwargs.get('is_released', False) and 'released_at' not in kwargs:
            kwargs['released_at'] = now
        
        if kwargs.get('is_announced', False) and 'announced_at' not in kwargs:
            kwargs['announced_at'] = now
            
        super().__init__(**kwargs)
    
    def calculate_release_time(self, campaign_start_time: datetime, release_cadence_hours: int) -> datetime:
        """Calculate when this challenge should be released based on campaign schedule."""
        from datetime import timedelta
        hours_offset = (self.order_position - 1) * release_cadence_hours
        return campaign_start_time + timedelta(hours=hours_offset)
    
    def should_be_released(self, campaign_start_time: datetime, release_cadence_hours: int) -> bool:
        """Check if this challenge should be released based on current time."""
        release_time = self.calculate_release_time(campaign_start_time, release_cadence_hours)
        return datetime.now(timezone.utc) >= release_time
    
    def __repr__(self) -> str:
        """String representation of the challenge."""
        status = "released" if self.is_released else "pending"
        return f"<Challenge(title='{self.title}', position={self.order_position}, status='{status}')>"


class ChallengeInput(Base):
    """Squad-specific input data for challenges.
    
    Stores generated inputs and expected results for challenges on a per-squad basis.
    All members of the same squad receive the same input data to ensure fairness
    and consistent problem-solving conditions.
    """
    
    __tablename__ = "challenge_inputs"
    
    # Compound primary key (challenge_id, squad_id)
    challenge_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
        doc="UUID of the challenge"
    )
    squad_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("squads.id", ondelete="CASCADE"),
        primary_key=True,
        doc="UUID of the squad"
    )
    
    # Generated data
    input_data: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Generated input data for the challenge (JSON string or plain text)"
    )
    result_data: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Expected result/solution for the input data"
    )
    
    # Note: created_at and updated_at are automatically added by Base class
    
    # Relationships
    challenge: Mapped["Challenge"] = relationship(
        "Challenge",
        doc="Challenge this input belongs to"
    )
    squad: Mapped["Squad"] = relationship(
        "Squad",
        doc="Squad this input is generated for"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_challenge_inputs_challenge_id", "challenge_id"),
        Index("ix_challenge_inputs_squad_id", "squad_id"),
        Index("ix_challenge_inputs_created_at", "created_at"),
    )
    
    # No custom __init__ needed - Base class handles timestamps
    
    def __repr__(self) -> str:
        """String representation of the challenge input."""
        return f"<ChallengeInput(challenge_id={self.challenge_id}, squad_id={self.squad_id})>"


class ScheduledMessage(Base):
    """Scheduled message definition for campaigns.
    
    Represents messages that are sent at specific times to campaign announcement channels.
    Unlike challenges, these messages have no interactive buttons and are informational only.
    """
    
    __tablename__ = "scheduled_messages"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique scheduled message identifier"
    )
    
    # Campaign relationship
    campaign_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Campaign this scheduled message belongs to"
    )
    
    # Message content
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="Message title"
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        doc="Detailed message description"
    )
    
    # Scheduling
    scheduled_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        doc="When this message should be sent"
    )
    
    # Status tracking
    is_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether this scheduled message has been sent"
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When the message was actually sent"
    )
    
    # Audit fields
    created_by: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username or identifier of who created this message"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the message was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the message was last modified"
    )
    
    # Relationships
    campaign: Mapped["Campaign"] = relationship(
        "Campaign",
        back_populates="scheduled_messages"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_scheduled_messages_campaign_id", "campaign_id"),
        Index("ix_scheduled_messages_scheduled_time", "scheduled_time"),
        Index("ix_scheduled_messages_is_sent", "is_sent"),
        Index("ix_scheduled_messages_sent_at", "sent_at"),
        Index("ix_scheduled_messages_campaign_time", "campaign_id", "scheduled_time"),
        # Validation constraints
        CheckConstraint("scheduled_time IS NOT NULL", name="ck_scheduled_messages_time_required"),
    )
    
    def __init__(self, **kwargs):
        """Initialize ScheduledMessage with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('description', '')
        kwargs.setdefault('is_sent', False)
        
        # Auto-set sent_at if is_sent is True
        if kwargs.get('is_sent', False) and 'sent_at' not in kwargs:
            kwargs['sent_at'] = now
            
        super().__init__(**kwargs)
    
    @property
    def is_due(self) -> bool:
        """Check if this scheduled message is due to be sent."""
        return not self.is_sent and datetime.now(timezone.utc) >= self.scheduled_time
    
    def __repr__(self) -> str:
        """String representation of the scheduled message."""
        status = "sent" if self.is_sent else "pending"
        return f"<ScheduledMessage(title='{self.title}', scheduled={self.scheduled_time}, status='{status}')>"