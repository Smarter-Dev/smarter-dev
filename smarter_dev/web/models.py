"""Database models for the Smarter Dev application."""

from __future__ import annotations

from datetime import datetime, timezone, date
from typing import Optional
from uuid import UUID, uuid4

from decimal import Decimal

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime, Date
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index, UniqueConstraint, CheckConstraint
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from smarter_dev.shared.database import Base


class CampaignSignup(Base):
    """Email/Discord signups for marketing campaigns (e.g. sudo launch).

    Separate from the Campaign model which tracks challenge competitions.
    """

    __tablename__ = "campaign_signups"
    __table_args__ = (
        UniqueConstraint(
            "campaign_slug", "email",
            name="uq_campaign_signups_slug_email",
        ),
        UniqueConstraint(
            "campaign_slug", "discord_id",
            name="uq_campaign_signups_slug_discord_id",
        ),
        CheckConstraint(
            "email IS NOT NULL OR discord_id IS NOT NULL",
            name="at_least_one_contact",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    campaign_slug: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(320),
        nullable=True,
    )
    discord_id: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
    )
    email_confirmed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    confirmation_token: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        unique=True,
        index=True,
    )


class UserProfile(Base):
    """Project-side profile fields not tracked on Skrift's User model.

    One row per user; `user_id` references `users(id)` via raw SQL FK in the
    migration (Skrift's `users` table lives on a separate metadata).
    """

    __tablename__ = "user_profiles"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        unique=True,
        index=True,
    )
    handle: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        unique=True,
    )
    bio: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    timezone: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SudoMembership(Base):
    """One row per sudo membership lifecycle event for a user.

    Despite the name, this is now an append-only history table: a user can
    have multiple rows over time (renewals, resubscribes after lapse,
    comps). The "current" membership for a user is the latest non-revoked
    row with ``expires_at > now()``; this invariant is enforced in app code,
    not the DB, so revoked / lapsed history can coexist for the same
    ``user_id``.

    Sources:
      - ``one_time``: a Polar one-time order (``billing_reason=purchase``) —
        the founder model. ``expires_at`` advances on stacked renewals;
        ``subscription_id`` and ``will_renew`` are null.
      - ``subscription``: a Polar subscription. ``expires_at`` mirrors
        ``current_period_end`` from the latest paid order (``order.paid``);
        ``will_renew`` mirrors ``cancel_at_period_end`` (messaging only).
      - ``comp``: hand-issued. No Polar IDs required.

    ``revoked_reason`` distinguishes refund / dispute / admin clamps from
    natural lapse (which has no reason set). ``refunded_at`` stays as a
    timestamp companion specifically for refunds.

    ``role`` is the offering the row grants: ``hacker`` (recurring) or
    ``founder`` (one-time). Founder grants a superset of Hacker.
    """

    __tablename__ = "sudo_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('hacker', 'founder')",
            name="ck_sudo_memberships_role",
        ),
        CheckConstraint(
            "source IN ('one_time', 'subscription', 'comp')",
            name="ck_sudo_memberships_source",
        ),
        CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN ('refund', 'dispute', 'admin')",
            name="ck_sudo_memberships_revoked_reason",
        ),
        Index("ix_sudo_memberships_user_id", "user_id"),
        Index("ix_sudo_memberships_customer_id", "customer_id"),
        Index("ix_sudo_memberships_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="one_time",
    )
    customer_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    checkout_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
    )
    order_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
    )
    subscription_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
    )
    price_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    amount_paid_cents: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    will_renew: Mapped[Optional[bool]] = mapped_column(
        Boolean,
        nullable=True,
    )
    purchased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    refunded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_reason: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SudoMembershipReminder(Base):
    """Per-membership renewal-reminder ledger.

    Each row is "reminder for membership X at threshold T (30 / 7 / 1
    days before expiry) was sent." UNIQUE(membership_id, days_before)
    lets the daily sweep blindly try-insert; on duplicate the IntegrityError
    means we already sent that threshold.
    """

    __tablename__ = "sudo_membership_reminders"
    __table_args__ = (
        UniqueConstraint(
            "membership_id", "days_before",
            name="uq_sudo_membership_reminders_membership_id_days_before",
        ),
        Index("ix_sudo_membership_reminders_membership_id", "membership_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    membership_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )
    days_before: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WebhookEventProcessed(Base):
    """Dedupe ledger for Polar webhook events.

    Polar delivers webhooks at-least-once, so the router records every
    successfully-verified delivery's ``webhook-id`` (standard-webhooks
    delivery id) in this table before dispatching; a duplicate ``event_id``
    short-circuits the handler with a 200. Keeps the table small by
    garbage-collecting old rows out-of-band.
    """

    __tablename__ = "webhook_events_processed"

    event_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_webhook_events_processed_processed_at", "processed_at"),
    )


class FeatureFlag(Base):
    """Database-backed feature flag with three operating modes.

    Modes: 'enabled' (everyone), 'admin_only' (visible only to admins),
    'disabled' (visible to nobody). Auto-created on first read of an
    unknown key with `mode='disabled'`.
    """

    __tablename__ = "feature_flags"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('enabled', 'admin_only', 'disabled')",
            name="ck_feature_flags_mode",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="disabled",
        server_default="disabled",
    )
    description: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


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
    announcement_channel: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc="Discord channel ID for squad announcements (optional)"
    )
    
    # Relationships
    challenge_submissions: Mapped[list["ChallengeSubmission"]] = relationship(
        "ChallengeSubmission",
        back_populates="squad",
        cascade="all, delete-orphan"
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
    # No ForeignKey: during the key-system migration this column may hold
    # Skrift-native key IDs (main DB, skrift.api_keys) as well as legacy
    # public.api_keys IDs, so it is a plain correlation column.
    api_key_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True),
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
    # Note: expires_at and is_sensitive already have column-level index=True
    __table_args__ = (
        Index("ix_help_conversations_guild_started", "guild_id", "started_at"),
        Index("ix_help_conversations_user_started", "user_id", "started_at"),
        Index("ix_help_conversations_session_started", "session_id", "started_at"),
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

    # Guild context (index defined in __table_args__)
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
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
    
    # User tagging settings
    enable_user_tagging: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether this agent performs topic classification for user tagging"
    )
    enable_responses: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this agent generates AI responses to posts"
    )
    notification_topics: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of topics this agent can classify posts into (max 25)"
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


class ForumNotificationTopic(Base):
    """Available notification topics for forum user tagging.
    
    Defines the topics that forum agents can classify posts into for
    user notifications. Each guild/forum combination can have up to 25 topics.
    """
    
    __tablename__ = "forum_notification_topics"

    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the notification topic"
    )

    # Guild and forum context (indexes defined in __table_args__)
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord guild (server) snowflake ID"
    )
    forum_channel_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord forum channel snowflake ID"
    )
    
    # Topic definition
    topic_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Name of the notification topic"
    )
    topic_description: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc="Optional description of what this topic covers"
    )
    
    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the topic was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the topic was last updated"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_forum_notification_topics_guild_id", "guild_id"),
        Index("ix_forum_notification_topics_forum_channel_id", "forum_channel_id"),
        Index("ix_forum_notification_topics_guild_forum", "guild_id", "forum_channel_id"),
        UniqueConstraint("guild_id", "forum_channel_id", "topic_name", name="uq_forum_notification_topics_guild_forum_topic"),
    )
    
    def __init__(self, **kwargs):
        """Initialize ForumNotificationTopic with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        super().__init__(**kwargs)
    
    def __repr__(self) -> str:
        """String representation of the notification topic."""
        return f"<ForumNotificationTopic(topic_name='{self.topic_name}', guild_id='{self.guild_id}')>"


class ForumUserSubscription(Base):
    """User subscriptions to forum notification topics.
    
    Tracks which users want to be notified about specific topics in
    specific forum channels, with configurable expiration times.
    """
    
    __tablename__ = "forum_user_subscriptions"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the subscription"
    )
    
    # User and guild context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord guild (server) snowflake ID"
    )
    user_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord user snowflake ID"
    )
    username: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username for audit purposes (literal @username format)"
    )
    forum_channel_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord forum channel snowflake ID"
    )
    
    # Subscription settings
    subscribed_topics: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of topic names the user is subscribed to"
    )
    notification_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Hours until user no longer wants notifications (-1 for forever)"
    )
    
    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the subscription was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the subscription was last updated"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_forum_user_subscriptions_guild_id", "guild_id"),
        Index("ix_forum_user_subscriptions_user_id", "user_id"),
        Index("ix_forum_user_subscriptions_forum_channel_id", "forum_channel_id"),
        Index("ix_forum_user_subscriptions_guild_forum", "guild_id", "forum_channel_id"),
        UniqueConstraint("guild_id", "user_id", "forum_channel_id", name="uq_forum_user_subscriptions_guild_user_forum"),
        CheckConstraint("notification_hours = -1 OR (notification_hours >= 1 AND notification_hours <= 8760)", name="ck_forum_user_subscriptions_notification_hours_valid"),
    )
    
    def __init__(self, **kwargs):
        """Initialize ForumUserSubscription with default timestamps and subscribed topics."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('subscribed_topics', [])
        super().__init__(**kwargs)
    
    @property
    def is_expired(self) -> bool:
        """Check if this subscription has expired based on notification_hours."""
        if self.notification_hours == -1:  # Forever
            return False
        
        from datetime import timedelta
        expiry_time = self.updated_at + timedelta(hours=self.notification_hours)
        return datetime.now(timezone.utc) > expiry_time
    
    def __repr__(self) -> str:
        """String representation of the user subscription."""
        return f"<ForumUserSubscription(username='{self.username}', guild_id='{self.guild_id}', topics={len(self.subscribed_topics)})>"

class Quest(Base):
    __tablename__ = "quests"

    # Identity
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique quest identifier",
    )

    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord guild (server) snowflake ID this quest belongs to",
    )

    # Human-facing content
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="Quest title",
    )

    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Quest instructions / prompt shown to users",
    )

    quest_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="daily",
        doc="Quest type (daily, weekly, one_off, etc.)",
    )

    # Execution / validation scripts
    python_script: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Reference Python solution or logic",
    )

    input_generator_script: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Script to generate quest input (optional)",
    )

    solution_validator_script: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Script to validate user submissions",
    )

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_quests_guild_id", "guild_id"),
    )

    def __repr__(self) -> str:
        return f"<Quest(title='{self.title}', guild_id='{self.guild_id}', type='{self.quest_type}')>"

class QuestInput(Base):
    __tablename__ = "quest_inputs"

    daily_quest_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("daily_quests.id", ondelete="CASCADE"),
        primary_key=True,
        doc="Daily quest instance this input belongs to",
    )

    input_data: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Generated quest input",
    )

    result_data: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Expected result for validation",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DailyQuest(Base):
    """Quest instance active for a specific date in a guild.

    Example:
    2025-12-10 → Quest X is active for this guild until end of day (UTC).
    """

    __tablename__ = "daily_quests"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique daily quest instance identifier",
    )

    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Discord guild (server) snowflake ID",
    )

    quest_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("quests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Quest template this instance is based on",
    )

    is_announced: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Whether this daily quest has been announced"
    )

    announced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="When the daily quest was announced"
    )

    # Rotation info
    active_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
        doc="Date this quest is active for (UTC-based)",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="UTC timestamp when this daily quest stops being valid",
    )

    # Soft-disable flag
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this daily quest instance is active",
    )

    quest: Mapped["Quest"] = relationship(
        "Quest",
        lazy="joined",
    )

    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "quest_id",
            "active_date",
            name="uq_daily_quests_per_day",
        ),
        Index(
            "ix_daily_quests_guild_date",
            "guild_id",
            "active_date",
        ),
    )

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

class QuestSubmission(Base):
    __tablename__ = "quest_submissions"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    daily_quest_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("daily_quests.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    guild_id: Mapped[str] = mapped_column(
        String,
        index=True,
        nullable=False,
    )

    squad_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        index=True,
        nullable=False,
    )

    user_id: Mapped[str] = mapped_column(
        String,
        index=True,
        nullable=False,
    )

    username: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )

    submitted_solution: Mapped[str] = mapped_column(Text, nullable=False)

    is_correct: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    is_first_success: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    points_earned: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_quest_submissions_daily_squad_first",
            "daily_quest_id",
            "squad_id",
            "is_first_success",
        ),
    )


class QuestProgress(Base):
    """Per-user completion tracking for a daily quest instance."""

    __tablename__ = "quest_progress"

    daily_quest_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("daily_quests.id", ondelete="CASCADE"),
        primary_key=True,
        doc="Daily quest instance this progress relates to",
    )
    user_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord user snowflake ID",
    )
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
        doc="Guild (server) where this progress applies",
    )

    completions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        doc="How many times the user has completed this daily quest today",
    )
    last_completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="When the user last completed this quest",
    )

    __table_args__ = (
        Index("ix_quest_progress_guild_user", "guild_id", "user_id"),
    )

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

        doc="Whether this challenge has been released to participants"
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,

        doc="Timestamp when the challenge was released"
    )
    
    # Announcement tracking
    is_announced: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,

        doc="Whether this challenge has been announced to Discord channels"
    )
    announced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,

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
    submissions: Mapped[list["ChallengeSubmission"]] = relationship(
        "ChallengeSubmission",
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
        doc="Detailed message description (sent to squad channels)"
    )
    announcement_channel_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Optional message for campaign announcement channels (if not set, description is used)"
    )
    
    # Scheduling
    scheduled_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,

        doc="When this message should be sent"
    )
    
    # Status tracking
    is_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,

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


class ChallengeSubmission(Base):
    """Model for tracking challenge solution submissions and success records."""
    
    __tablename__ = "challenge_submissions"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID,
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the submission"
    )
    
    # Foreign keys
    challenge_id: Mapped[UUID] = mapped_column(
        PostgresUUID,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Reference to the challenge"
    )
    squad_id: Mapped[UUID] = mapped_column(
        PostgresUUID,
        ForeignKey("squads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Reference to the squad making the submission"
    )
    
    # User who submitted
    user_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        doc="Discord user ID who submitted the solution"
    )
    username: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username of the submitting user for audit purposes"
    )
    
    # Submission data
    submitted_solution: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="The solution text submitted by the user"
    )
    is_correct: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        doc="Whether the submitted solution matches the expected result"
    )
    is_first_success: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Whether this is the first correct submission for this squad/challenge"
    )
    points_earned: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Points earned for this submission (only for correct first submissions)"
    )
    
    # Timestamps
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the solution was submitted"
    )
    
    # Relationships
    challenge: Mapped["Challenge"] = relationship("Challenge", back_populates="submissions")
    squad: Mapped["Squad"] = relationship("Squad", back_populates="challenge_submissions")
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_challenge_submissions_challenge_squad", "challenge_id", "squad_id"),
        Index("ix_challenge_submissions_user_submitted", "user_id", "submitted_at"),
        Index("ix_challenge_submissions_first_success", "is_first_success", "submitted_at"),
    )
    
    def __repr__(self) -> str:
        """String representation of the challenge submission."""
        status = "correct" if self.is_correct else "incorrect"
        first = " (first success)" if self.is_first_success else ""
        return f"<ChallengeSubmission(challenge_id={self.challenge_id}, squad_id={self.squad_id}, status='{status}'{first})>"


class SquadSaleEvent(Base):
    """Squad sale event model for timed discount events on squad joining/switching.
    
    Allows administrators to create special sale events with configurable discounts
    for squad joining and switching costs. Events are time-based with automatic
    start/end based on duration.
    """
    
    __tablename__ = "squad_sale_events"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the sale event"
    )
    
    # Guild context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord guild (server) snowflake ID"
    )
    
    # Event metadata
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Name of the sale event"
    )
    description: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="",
        doc="Description of the sale event"
    )
    
    # Event timing
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,

        doc="When the sale event starts"
    )
    duration_hours: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Duration of the sale event in hours"
    )
    
    # Discount configuration
    join_discount_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Percentage discount for joining a squad (0-100)"
    )
    switch_discount_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Percentage discount for switching squads (0-100)"
    )
    
    # Event status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this sale event is active"
    )
    
    # Audit fields
    created_by: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username or identifier of who created this event"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the event was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the event was last modified"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_squad_sale_events_guild_id", "guild_id"),
        Index("ix_squad_sale_events_guild_active", "guild_id", "is_active"),
        Index("ix_squad_sale_events_start_time", "start_time"),
        Index("ix_squad_sale_events_created_by", "created_by"),
        UniqueConstraint("guild_id", "name", name="uq_squad_sale_events_guild_name"),
        # Validation constraints
        CheckConstraint("join_discount_percent >= 0 AND join_discount_percent <= 100", name="ck_squad_sale_events_join_discount_range"),
        CheckConstraint("switch_discount_percent >= 0 AND switch_discount_percent <= 100", name="ck_squad_sale_events_switch_discount_range"),
        CheckConstraint("duration_hours >= 1", name="ck_squad_sale_events_duration_positive"),
    )
    
    def __init__(self, **kwargs):
        """Initialize SquadSaleEvent with default timestamps."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('description', '')
        kwargs.setdefault('join_discount_percent', 0)
        kwargs.setdefault('switch_discount_percent', 0)
        kwargs.setdefault('is_active', True)
        super().__init__(**kwargs)
    
    @property
    def end_time(self) -> datetime:
        """Calculate when the sale event ends."""
        from datetime import timedelta
        return self.start_time + timedelta(hours=self.duration_hours)
    
    @property
    def is_currently_active(self) -> bool:
        """Check if the sale event is currently active based on time and status."""
        if not self.is_active:
            return False
        now = datetime.now(timezone.utc)
        return self.start_time <= now <= self.end_time
    
    @property
    def days_until_start(self) -> Optional[int]:
        """Get days until sale starts (None if already started)."""
        if self.has_started:
            return None
        delta = self.start_time - datetime.now(timezone.utc)
        return delta.days
    
    @property
    def has_started(self) -> bool:
        """Check if the sale event has started."""
        return datetime.now(timezone.utc) >= self.start_time
    
    @property
    def has_ended(self) -> bool:
        """Check if the sale event has ended."""
        return datetime.now(timezone.utc) > self.end_time
    
    @property
    def time_remaining_hours(self) -> Optional[int]:
        """Get hours remaining in the sale (None if not active)."""
        if not self.is_currently_active:
            return None
        delta = self.end_time - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() // 3600))
    
    def calculate_discounted_cost(self, original_cost: int, is_switch: bool) -> int:
        """Calculate the discounted cost for squad joining/switching.
        
        Args:
            original_cost: The original cost before discount
            is_switch: True if this is a squad switch, False if first join
            
        Returns:
            The discounted cost
        """
        if not self.is_currently_active:
            return original_cost
        
        discount_percent = self.switch_discount_percent if is_switch else self.join_discount_percent
        if discount_percent == 0:
            return original_cost
        
        discount_amount = int(original_cost * discount_percent / 100)
        return max(0, original_cost - discount_amount)
    
    def __repr__(self) -> str:
        """String representation of the squad sale event."""
        status = "active" if self.is_currently_active else "inactive"
        return f"<SquadSaleEvent(name='{self.name}', guild_id='{self.guild_id}', status='{status}')>"


class RepeatingMessage(Base):
    """Repeating scheduled message model for guild channels.
    
    Allows administrators to create messages that repeat at regular intervals
    in specified Discord channels, with optional role mentions and UTC timing.
    """
    
    __tablename__ = "repeating_messages"
    
    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the repeating message"
    )
    
    # Guild and channel context
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord guild (server) snowflake ID"
    )
    channel_id: Mapped[str] = mapped_column(
        String,
        nullable=False,

        doc="Discord channel snowflake ID where messages are sent"
    )
    
    # Message content
    message_content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="The message text to send repeatedly"
    )
    role_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Optional Discord role ID to mention with the message"
    )
    
    # Scheduling configuration
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        doc="UTC datetime when the first message should be sent"
    )
    interval_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Minutes between repeated messages (minimum 1)"
    )
    
    # Runtime state
    next_send_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,

        doc="UTC datetime when the next message should be sent"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        doc="Whether this repeating message is active"
    )
    
    # Statistics
    total_sent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Total number of messages sent successfully"
    )
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When the last message was successfully sent"
    )
    
    # Audit fields
    created_by: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="Username or identifier of who created this repeating message"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the repeating message was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the repeating message was last modified"
    )
    
    # Database constraints and indexes
    __table_args__ = (
        Index("ix_repeating_messages_guild_id", "guild_id"),
        Index("ix_repeating_messages_channel_id", "channel_id"),
        Index("ix_repeating_messages_next_send_time", "next_send_time"),
        Index("ix_repeating_messages_guild_active", "guild_id", "is_active"),
        Index("ix_repeating_messages_due", "is_active", "next_send_time"),
        Index("ix_repeating_messages_created_by", "created_by"),
        # Validation constraints
        CheckConstraint("interval_minutes >= 1", name="ck_repeating_messages_interval_positive"),
        CheckConstraint("total_sent >= 0", name="ck_repeating_messages_total_sent_non_negative"),
    )
    
    def __init__(self, **kwargs):
        """Initialize RepeatingMessage with default timestamps and next_send_time."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('total_sent', 0)
        kwargs.setdefault('is_active', True)
        
        # Set next_send_time to start_time if not provided
        if 'next_send_time' not in kwargs and 'start_time' in kwargs:
            kwargs['next_send_time'] = kwargs['start_time']
        
        super().__init__(**kwargs)
    
    @property
    def is_due(self) -> bool:
        """Check if this repeating message is due to be sent."""
        return self.is_active and datetime.now(timezone.utc) >= self.next_send_time
    
    @property
    def has_started(self) -> bool:
        """Check if the repeating message schedule has started."""
        return datetime.now(timezone.utc) >= self.start_time
    
    def calculate_next_send_time(self) -> datetime:
        """Calculate the next send time maintaining the original schedule interval.
        
        Rule: If we missed xx:11 and it's now xx:13, the next send should be xx:13 (xx:11 + 2min),
        NOT xx:15 (xx:13 + 2min). Always calculate from the original missed time.
        """
        from datetime import timedelta
        import logging
        
        logger = logging.getLogger(__name__)
        now = datetime.now(timezone.utc)
        
        old_next_send_time = self.next_send_time
        
        # Calculate next send from the ORIGINAL scheduled time (not current time)
        candidate_next_send_time = self.next_send_time + timedelta(minutes=self.interval_minutes)
        
        # If we're still behind (multiple missed intervals), advance to current time
        # but maintain the original schedule pattern
        while candidate_next_send_time < now:
            candidate_next_send_time += timedelta(minutes=self.interval_minutes)
        
        print(f"🔄 SCHEDULE CALC for {self.id}: missed_time={old_next_send_time}, now={now}, interval={self.interval_minutes}min, next={candidate_next_send_time}")
        logger.warning(f"SCHEDULE CALC for {self.id}: missed_time={old_next_send_time}, now={now}, interval={self.interval_minutes}min, next={candidate_next_send_time}")
        
        return candidate_next_send_time
    
    def update_after_send(self) -> None:
        """Update statistics and next send time after successful message send."""
        import logging
        
        logger = logging.getLogger(__name__)
        now = datetime.now(timezone.utc)
        old_next_send_time = self.next_send_time
        
        self.total_sent += 1
        self.last_sent_at = now
        self.next_send_time = self.calculate_next_send_time()
        self.updated_at = now
        
        print(f"📤 UPDATE AFTER SEND {self.id}: sent_at={now}, old_next={old_next_send_time}, new_next={self.next_send_time}, total={self.total_sent}")
        logger.warning(f"UPDATE AFTER SEND {self.id}: sent_at={now}, old_next={old_next_send_time}, new_next={self.next_send_time}, total={self.total_sent}")
    
    def get_formatted_message(self) -> str:
        """Get the message content formatted with optional role mention."""
        if self.role_id:
            return f"{self.message_content}\n\n<@&{self.role_id}>"
        return self.message_content
    
    def __repr__(self) -> str:
        """String representation of the repeating message."""
        status = "active" if self.is_active else "inactive"
        return f"<RepeatingMessage(guild_id='{self.guild_id}', channel_id='{self.channel_id}', status='{status}')>"


class AuditLogConfig(Base):
    """Audit log configuration per guild for Discord event logging.

    Stores guild-specific settings for audit logging events like member join/leave,
    bans, message edits/deletes, and user changes. Logs are sent as embeds to the
    configured audit channel.
    """

    __tablename__ = "audit_log_configs"

    # Primary key
    guild_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord guild (server) snowflake ID"
    )

    # Audit channel configuration
    audit_channel_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Discord channel ID where audit logs are sent"
    )

    # Member event logging
    log_member_join: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="Log when members join the guild"
    )
    log_member_leave: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="Log when members leave the guild"
    )
    log_member_ban: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="Log when members are banned"
    )
    log_member_unban: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="Log when members are unbanned"
    )

    # Message event logging
    log_message_edit: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Log when messages are edited"
    )
    log_message_delete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Log when messages are deleted"
    )

    # User change event logging
    log_username_change: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Log when users change their username"
    )
    log_nickname_change: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Log when users change their nickname in the guild"
    )
    log_role_change: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Log when user roles are added or removed"
    )

    # Audit timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Timestamp when the config was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="Timestamp when the config was last updated"
    )

    # Database constraints and indexes
    __table_args__ = (
        Index("ix_audit_log_configs_guild_id", "guild_id"),
    )

    def __init__(self, **kwargs):
        """Initialize AuditLogConfig with default values."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('log_member_join', True)
        kwargs.setdefault('log_member_leave', True)
        kwargs.setdefault('log_member_ban', True)
        kwargs.setdefault('log_member_unban', True)
        kwargs.setdefault('log_message_edit', False)
        kwargs.setdefault('log_message_delete', False)
        kwargs.setdefault('log_username_change', False)
        kwargs.setdefault('log_nickname_change', False)
        kwargs.setdefault('log_role_change', False)
        super().__init__(**kwargs)

    @classmethod
    def get_defaults(cls, guild_id: str) -> 'AuditLogConfig':
        """Get default configuration for a guild.

        Args:
            guild_id: Discord guild ID

        Returns:
            AuditLogConfig instance with default values
        """
        return cls(guild_id=guild_id)

    def __repr__(self) -> str:
        """String representation of the audit log config."""
        return f"<AuditLogConfig(guild_id='{self.guild_id}', channel='{self.audit_channel_id}')>"


class AdventOfCodeConfig(Base):
    """Advent of Code configuration per guild for daily challenge threads.

    Stores guild-specific settings for automatic creation of daily Advent of Code
    discussion threads in a designated forum channel. Threads are created at midnight
    EST when each day's challenge is released.
    """

    __tablename__ = "advent_of_code_configs"

    # Primary key
    guild_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord guild (server) snowflake ID"
    )

    # Forum channel configuration
    forum_channel_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Discord forum channel ID where AoC threads are created"
    )

    # Feature toggle
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Whether automatic thread creation is enabled"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="When this config was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="When this config was last updated"
    )

    # Relationship to posted threads
    posted_threads: Mapped[list["AdventOfCodeThread"]] = relationship(
        "AdventOfCodeThread",
        back_populates="config",
        cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs):
        """Initialize AdventOfCodeConfig with default values."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('is_active', False)
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        """String representation of the AoC config."""
        return f"<AdventOfCodeConfig(guild_id='{self.guild_id}', channel='{self.forum_channel_id}', active={self.is_active})>"


class AdventOfCodeThread(Base):
    """Tracks Advent of Code threads that have been created.

    Records which day's threads have been successfully posted to prevent
    duplicate thread creation and provide audit history.
    """

    __tablename__ = "advent_of_code_threads"

    # Primary key
    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        doc="Unique identifier for the thread record"
    )

    # Foreign key to config
    guild_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("advent_of_code_configs.guild_id", ondelete="CASCADE"),
        nullable=False,
        doc="Discord guild (server) snowflake ID"
    )

    # Day tracking
    year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Advent of Code year"
    )
    day: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Day of the challenge (1-25)"
    )

    # Thread details
    thread_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord thread/post snowflake ID"
    )
    thread_title: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Title of the created thread"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="When the thread was created"
    )

    # Relationship back to config
    config: Mapped["AdventOfCodeConfig"] = relationship(
        "AdventOfCodeConfig",
        back_populates="posted_threads"
    )

    # Constraints
    __table_args__ = (
        # Ensure unique thread per guild/year/day
        UniqueConstraint("guild_id", "year", "day", name="uq_aoc_thread_guild_year_day"),
        Index("ix_aoc_threads_guild_id", "guild_id"),
        Index("ix_aoc_threads_year_day", "year", "day"),
    )

    def __init__(self, **kwargs):
        """Initialize AdventOfCodeThread."""
        kwargs.setdefault('id', uuid4())
        kwargs.setdefault('created_at', datetime.now(timezone.utc))
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        """String representation of the AoC thread record."""
        return f"<AdventOfCodeThread(guild='{self.guild_id}', year={self.year}, day={self.day})>"



class AttachmentFilterConfig(Base):
    """Attachment filter configuration per guild for file type filtering.

    Stores guild-specific settings for filtering message attachments based on
    file extensions using a three-tier approach:
    - Ignored extensions: Completely allowed, no action taken
    - Warn extensions: Send a warning but don't delete
    - All others (blocked): Delete message and send warning

    Users with manage_messages permission are exempt from deletion (warning only).
    """

    __tablename__ = "attachment_filter_configs"

    # Primary key
    guild_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord guild (server) snowflake ID"
    )

    # Feature toggle
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Whether attachment filtering is enabled"
    )

    # Ignored file extensions - completely allowed, no action
    ignored_extensions: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of ignored file extensions (e.g., ['.png', '.jpg']). No action taken."
    )

    # Warn file extensions - send warning but don't delete
    warn_extensions: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="List of file extensions that trigger a warning only (e.g., ['.zip', '.rar'])."
    )

    # Custom message for warn-list files
    warn_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        default=None,
        doc="Custom message for warn-list files (supports {user}, {extension}, {filename} placeholders)"
    )

    # Custom message for blocked/deleted files
    delete_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        default=None,
        doc="Custom message for blocked files (supports {user}, {extension}, {filename} placeholders)"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="When this config was created"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="When this config was last updated"
    )

    # Database constraints and indexes
    __table_args__ = (
        Index("ix_attachment_filter_configs_guild_id", "guild_id"),
    )

    def __init__(self, **kwargs):
        """Initialize AttachmentFilterConfig with default values."""
        now = datetime.now(timezone.utc)
        kwargs.setdefault('created_at', now)
        kwargs.setdefault('updated_at', now)
        kwargs.setdefault('is_active', False)
        kwargs.setdefault('ignored_extensions', [])
        kwargs.setdefault('warn_extensions', [])
        super().__init__(**kwargs)

    @classmethod
    def get_defaults(cls, guild_id: str) -> 'AttachmentFilterConfig':
        """Get default configuration for a guild.

        Args:
            guild_id: Discord guild ID

        Returns:
            AttachmentFilterConfig instance with default values
        """
        return cls(guild_id=guild_id)

    def get_message(self, user_mention: str, extension: str, filename: str, is_blocked: bool = True) -> str:
        """Get the formatted message for warn or delete actions.

        Args:
            user_mention: The user mention string (e.g., <@123456>)
            extension: The file extension
            filename: The filename
            is_blocked: True if the file was deleted (blocked), False if just warned

        Returns:
            Formatted message
        """
        if is_blocked:
            if self.delete_message:
                return self.delete_message.format(
                    user=user_mention,
                    extension=extension,
                    filename=filename
                )
            return f"{user_mention}, your message was removed because the file type ({extension}) is not allowed. Please use an approved file format."
        else:
            if self.warn_message:
                return self.warn_message.format(
                    user=user_mention,
                    extension=extension,
                    filename=filename
                )
            return f"{user_mention}, your attachment ({extension}) requires caution. Please ensure you trust the source of this file."

    def __repr__(self) -> str:
        """String representation of the attachment filter config."""
        return f"<AttachmentFilterConfig(guild_id='{self.guild_id}', active={self.is_active})>"


class ResearchSession(Base):
    """A research session created by the Scan research agent."""

    __tablename__ = "research_sessions"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(
        String(250), nullable=True, unique=True, index=True,
    )
    user_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    guild_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    channel_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )
    response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sources: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=list)
    tool_log: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=list)
    followups: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=list)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    pipeline_mode: Mapped[str] = mapped_column(String(20), default="lite")
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    model_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)

    def __repr__(self) -> str:
        return f"<ResearchSession(id='{self.id}', status='{self.status}')>"


class ScanUserProfile(Base):
    """Evolving profile of a Scan user built from their research queries.

    Each time a user submits a query, Gemini 3 Flash evaluates it against the
    existing profile and writes an updated 2-5 paragraph summary describing
    the user's interests, skill level, and research patterns.
    """

    __tablename__ = "scan_user_profiles"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True,
    )
    profile: Mapped[str] = mapped_column(Text, nullable=False, default="")
    technologies: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    recent_queries: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suggested_queries: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    opt_out_narrative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    opt_out_technologies: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    def __repr__(self) -> str:
        return f"<ScanUserProfile(user_id='{self.user_id}', queries={self.query_count})>"


class ScanServiceUsage(Base):
    """Internal service usage tracking for Scan background tasks.

    Tracks costs that are not user-facing (e.g. profiler LLM calls) separately
    from per-session costs.  Each row is a single invocation of an internal agent.
    """

    __tablename__ = "scan_service_usage"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    task_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    session_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True,
    )

    def __repr__(self) -> str:
        return f"<ScanServiceUsage(task='{self.task_type}', cost={self.cost_usd})>"


class ModerationConfig(Base):
    """Per-guild configuration for AI moderation triage.

    When a user mentions a monitored role, the bot reads chat history and uses
    an AI triage agent to freeze dangerous situations (timeout, purge, delete)
    while waiting for human moderators to arrive.
    """

    __tablename__ = "moderation_configs"

    guild_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        doc="Discord guild (server) snowflake ID",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Whether moderation monitoring is enabled",
    )
    monitored_role_ids: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
        doc="List of Discord role ID strings to monitor for mentions",
    )
    instructions: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="System prompt / moderation instructions given to the AI agent",
    )
    enabled_tools: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: ["timeout", "purge", "delete"],
        server_default='["timeout", "purge", "delete"]',
        doc="List of action tool names enabled: timeout, purge, delete",
    )
    response_channel_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Channel where triage reports are posted for human moderator review",
    )
    context_message_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=25,
        doc="Number of recent messages to fetch for context",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ModerationConfig(guild_id='{self.guild_id}', active={self.is_active})>"


class ModerationAction(Base):
    """Unified tracking table for all moderation actions.

    Records warns, kicks, bans, unbans, and timeouts regardless of source
    (AI agent, manual slash command, or Discord audit log detection).
    """

    __tablename__ = "moderation_actions"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    guild_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord guild snowflake ID",
    )
    target_user_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Discord user ID of the actioned user",
    )
    target_username: Mapped[str] = mapped_column(
        String,
        nullable=False,
        doc="Username snapshot at action time",
    )
    moderator_user_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Discord user ID of moderator (null for AI actions)",
    )
    moderator_username: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Moderator username snapshot",
    )
    action_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="Action type: warn, kick, ban, unban, timeout",
    )
    reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Reason for the moderation action",
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Duration in seconds (for timeouts)",
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="ai",
        server_default="ai",
        doc="Action source: ai, manual, audit_log",
    )
    channel_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Channel where the trigger occurred",
    )
    trigger_message_id: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        doc="Message ID that triggered AI moderation",
    )
    ai_context_summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="AI's summary of the situation (AI-initiated actions only)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_mod_actions_guild_user", "guild_id", "target_user_id"),
        Index("ix_mod_actions_guild_type", "guild_id", "action_type"),
    )

    def __repr__(self) -> str:
        return f"<ModerationAction(type='{self.action_type}', target='{self.target_username}', source='{self.source}')>"


class TrackedLinkCounter(Base):
    """Generic outbound-link click counter.

    Increments via /v2/api/track-click when a frontend `data-track-key` anchor is
    clicked (see themes/smarterdev/static/js/click-tracker.js). Keyed by a stable
    string so callers control the namespace (e.g. "vibe:course:fireship-...").
    """

    __tablename__ = "tracked_link_counters"

    key: Mapped[str] = mapped_column(
        String(200),
        primary_key=True,
    )
    url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
    )
    last_clicked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<TrackedLinkCounter(key='{self.key}', count={self.count})>"


# ─── /resources/* content ───────────────────────────────────────────────────


class ResourceDirectory(Base):
    """One of the five /resources/* directory pages."""

    __tablename__ = "resource_directories"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    track_key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False, unique=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    categories: Mapped[list["ResourceCategory"]] = relationship(
        back_populates="directory",
        cascade="all, delete-orphan",
        order_by="ResourceCategory.sort_order",
    )
    spine_placements: Mapped[list["ResourceDirectorySpine"]] = relationship(
        back_populates="directory",
        cascade="all, delete-orphan",
        order_by="ResourceDirectorySpine.sort_order",
    )
    creators: Mapped[list["ResourceCreator"]] = relationship(
        back_populates="directory",
        cascade="all, delete-orphan",
        order_by="ResourceCreator.sort_order",
    )
    faqs: Mapped[list["ResourceFaq"]] = relationship(
        back_populates="directory",
        cascade="all, delete-orphan",
        order_by="ResourceFaq.sort_order",
    )

    def __repr__(self) -> str:
        return f"<ResourceDirectory(slug='{self.slug}')>"


class ResourceCategory(Base):
    """Section inside a directory ('Databases', 'Caching', etc.)."""

    __tablename__ = "resource_categories"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    directory_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_directories.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    intro_html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    directory: Mapped[ResourceDirectory] = relationship(back_populates="categories")
    tools: Mapped[list["ResourceTool"]] = relationship(
        back_populates="category",
        cascade="all, delete-orphan",
        order_by="ResourceTool.sort_order",
    )

    __table_args__ = (
        UniqueConstraint("directory_id", "slug", name="uq_resource_categories_dir_slug"),
    )


class ResourceTool(Base):
    """A peer tool inside a category (Postgres, Terraform, ...)."""

    __tablename__ = "resource_tools"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    category_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    home_track_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    blurb: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    category: Mapped[ResourceCategory] = relationship(back_populates="tools")
    source_placements: Mapped[list["ResourceToolSource"]] = relationship(
        back_populates="tool",
        cascade="all, delete-orphan",
        order_by="ResourceToolSource.sort_order",
    )

    __table_args__ = (
        UniqueConstraint("category_id", "slug", name="uq_resource_tools_cat_slug"),
    )


class ResourceSource(Base):
    """A learning resource we link out to: tutorial, article, book, talk, course."""

    __tablename__ = "resource_sources"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    track_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    byline: Mapped[str] = mapped_column(String(256), nullable=False)
    blurb: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    learning_type: Mapped[str] = mapped_column(String(32), nullable=False)
    published_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    first_indexed_at: Mapped[date] = mapped_column(Date, nullable=False)
    jina_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    jina_fetched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ResourceDirectorySpine(Base):
    """A source's placement in a directory's spine."""

    __tablename__ = "resource_directory_spine"

    directory_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_directories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    directory: Mapped[ResourceDirectory] = relationship(back_populates="spine_placements")
    source: Mapped[ResourceSource] = relationship()


class ResourceToolSource(Base):
    """A source's placement under one or more specific tools."""

    __tablename__ = "resource_tool_sources"

    tool_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_tools.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    tool: Mapped[ResourceTool] = relationship(back_populates="source_placements")
    source: Mapped[ResourceSource] = relationship()


class ResourceCreator(Base):
    """A 'Creators to follow' entry, scoped per directory."""

    __tablename__ = "resource_creators"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    directory_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_directories.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    handle: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    track_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    blurb: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    directory: Mapped[ResourceDirectory] = relationship(back_populates="creators")

    __table_args__ = (
        UniqueConstraint("directory_id", "slug", name="uq_resource_creators_dir_slug"),
    )


class ResourceFaq(Base):
    """Per-directory FAQ entry."""

    __tablename__ = "resource_faqs"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    directory_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("resource_directories.id", ondelete="CASCADE"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source_label: Mapped[str] = mapped_column(String(256), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_track_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    directory: Mapped[ResourceDirectory] = relationship(back_populates="faqs")


# ---------------------------------------------------------------------------
# Agent conversations — persisted Q&A sessions for the /resources agent (and
# future agent_types). Schema is agent-type-agnostic; the rendering template
# branches on AgentConversation.agent_type.
# ---------------------------------------------------------------------------


class AgentConversation(Base):
    """A persisted conversation between a user and a Skrift agent."""

    __tablename__ = "agent_conversations"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # FK to skrift.users(id); declared manually in the migration since the
    # Skrift User model lives on a separate Base metadata.
    owner_user_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentMessage.sequence",
    )

    __table_args__ = (
        Index("ix_agent_conversations_owner_created", "owner_user_id", "created_at"),
    )


class AgentMessage(Base):
    """One turn (user or assistant) within an AgentConversation."""

    __tablename__ = "agent_messages"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    conversation: Mapped[AgentConversation] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_agent_messages_conv_seq"),
    )


# ---------------------------------------------------------------------------
# Discord chat agent — engagement / turn / compaction tables for the
# operator dashboard. One engagement = one ChannelEngine lifecycle; one
# turn = one agent fire (SendResponse OR NoResponse).
# ---------------------------------------------------------------------------


class ChatAgentEngagement(Base):
    """One row per Discord chat-agent engine lifecycle in a channel."""

    __tablename__ = "chat_agent_engagements"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )

    # Discord context
    guild_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    guild_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    channel_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # Activation trigger snapshot
    activation_user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    activation_username: Mapped[str] = mapped_column(String(100), nullable=False)
    activation_message_id: Mapped[str] = mapped_column(String, nullable=False)

    # Lifecycle
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deactivation_reason: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        doc=(
            "no_response_quota / inactivity / continue_watching_false / "
            "stop_phrase / max_runtime / shutdown / crash"
        ),
    )

    # Denormalised latest values for the list view
    last_topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Aggregate tokens across all turns
    total_chat_tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chat_tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_compaction_tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_compaction_tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_voice_tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_voice_tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Aggregate USD cost — broken out by bucket so operators can see
    # which model is driving spend. ``total_cost_usd`` is the convenience
    # sum of the three.
    total_chat_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    total_voice_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    total_compaction_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )

    turns: Mapped[list["ChatAgentTurn"]] = relationship(
        back_populates="engagement",
        cascade="all, delete-orphan",
        order_by="ChatAgentTurn.started_at",
    )

    __table_args__ = (
        Index(
            "ix_chat_agent_engagements_guild_started", "guild_id", "started_at"
        ),
        Index(
            "ix_chat_agent_engagements_channel_started",
            "channel_id",
            "started_at",
        ),
        Index(
            "ix_chat_agent_engagements_user_started",
            "activation_user_id",
            "started_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ChatAgentEngagement(id='{self.id}', channel='{self.channel_name}', "
            f"started_at='{self.started_at}', reason='{self.deactivation_reason}')>"
        )


class ChatAgentTurn(Base):
    """One row per chat-agent fire (SendResponse OR NoResponse)."""

    __tablename__ = "chat_agent_turns"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    engagement_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("chat_agent_engagements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    request_id: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="initial or followup"
    )
    output_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="send_response or no_response",
    )

    # Snapshots of what the agent saw and produced
    triggering_messages: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        doc=(
            "List of `Message` dicts: activation_message on initial, "
            "new_messages on followup."
        ),
    )
    agent_output: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        doc=(
            "Full SendResponse or NoResponse model_dump — includes message, "
            "voice_summary, voice_instruction, topic, notes, reply_to_message_id, "
            "continue_watching, kind."
        ),
    )
    model_messages_delta: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        doc=(
            "Pydantic AI messages added during this turn "
            "(result.new_messages() JSON-serialised). Used to render tool "
            "calls/returns inline in the timeline."
        ),
    )

    # Timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Token / cost bucket: chat
    chat_tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chat_tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chat_model_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    chat_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )

    # Token / cost bucket: voice (zero unless voice_summary was sent)
    voice_tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    voice_tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    voice_model_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    voice_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    voice_sent_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    voice_send_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    engagement: Mapped[ChatAgentEngagement] = relationship(back_populates="turns")
    compaction_events: Mapped[list["ChatAgentCompactionEvent"]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
        order_by="ChatAgentCompactionEvent.id",
    )

    def __repr__(self) -> str:
        return (
            f"<ChatAgentTurn(id='{self.id}', kind='{self.turn_kind}', "
            f"output='{self.output_kind}', tokens_in={self.chat_tokens_input})>"
        )


class ChatAgentCompactionEvent(Base):
    """One row per part that the history compactor summarised, during a turn."""

    __tablename__ = "chat_agent_compaction_events"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    turn_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("chat_agent_turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_kind: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        doc="user_prompt / assistant_text / tool_call_args / tool_return",
    )
    tool_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    # Full content — no truncation. Operator wants to review the
    # summariser's quality and can't do that with a snippet.
    original_content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    original_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    chars_saved: Mapped[int] = mapped_column(Integer, nullable=False)

    summarizer_tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summarizer_tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summarizer_model_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    summarizer_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )

    turn: Mapped[ChatAgentTurn] = relationship(back_populates="compaction_events")

    def __repr__(self) -> str:
        return (
            f"<ChatAgentCompactionEvent(kind='{self.event_kind}', "
            f"{self.original_chars}c->{self.summary_chars}c)>"
        )


# ---------------------------------------------------------------------------
# Blog metadata — sidecar tables for Skrift's page-type=blog.
#
# Skrift's Page model lives on its own metadata, so we can't add columns to it
# from this Base. Instead we keep blog-specific fields (agent-author flag,
# reviewer, tags) on adjacent project-owned tables and join in the view layer.
# ---------------------------------------------------------------------------


class AuthorProfile(Base):
    """Per-user blog publishing profile (extension of Skrift's User)."""

    __tablename__ = "author_profiles"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # FK to skrift.users(id); declared raw in the migration.
    user_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False, unique=True
    )
    is_agent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class BlogPostMeta(Base):
    """Blog-specific sidecar fields for a Skrift page with type='blog'."""

    __tablename__ = "blog_post_meta"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # FK to skrift.pages(id); declared raw in the migration.
    page_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False, unique=True
    )
    # FK to skrift.users(id); declared raw in the migration.
    reviewed_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )


class Tag(Base):
    """A taxonomy tag, shared across blog posts (and reusable later)."""

    __tablename__ = "tags"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class BlogPostTag(Base):
    """Many-to-many join between blog posts (pages) and tags."""

    __tablename__ = "blog_post_tags"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # FK to skrift.pages(id); declared raw in the migration.
    page_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    tag_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "page_id", "tag_id", name="uq_blog_post_tags_page_tag"
        ),
        Index("ix_blog_post_tags_page_id", "page_id"),
    )


# ---------------------------------------------------------------------------
# Blogging-agent scout — candidate topics surfaced by the Discord chat agent.
# ---------------------------------------------------------------------------


class CandidateBlogTopic(Base):
    """A blog-post idea filed by an agent (initially: the Discord chat agent).

    Each row is one idea pitched by an agent in response to a chat turn. A
    human triages the queue in the admin: kept → drafted → eventually
    published, or discarded.
    """

    __tablename__ = "candidate_blog_topics"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )

    # Origin. Engagement / turn FKs declared raw in the migration since
    # they're on the same project Base but we keep the pattern for symmetry
    # with the skrift.users / skrift.pages FKs below.
    engagement_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )
    turn_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )
    surfaced_by: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="chat-agent"
    )
    surfaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Content — hypothesis-driven pipeline shape (matches what Scout emits).
    headline: Mapped[str] = mapped_column(String(255), nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    evidence: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Lifecycle: new -> kept -> drafted -> discarded
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="new"
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # FK to skrift.users(id); declared raw in the migration.
    reviewed_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )
    # FK to skrift.pages(id); declared raw in the migration. Wired up in a
    # later pass when the "promote to draft" action lands.
    blog_page_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_candidate_blog_topics_status_surfaced",
            "status",
            "surfaced_at",
        ),
        Index("ix_candidate_blog_topics_engagement_id", "engagement_id"),
    )


# ---------------------------------------------------------------------------
# Authoring pipeline — admin-triggered multi-stage agent run that produces a
# blog post. The actual audit log is owned by Skrift's per-agent event log
# (stream `agents:run:{session_id}`); this row just carries the bookkeeping.
# ---------------------------------------------------------------------------


class AuthoringPipelineRun(Base):
    """One admin-triggered execution of the 5-stage blogging pipeline."""

    __tablename__ = "authoring_pipeline_runs"

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )

    # queued -> running -> completed | failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="queued"
    )

    # FK to skrift.users(id); declared raw in the migration.
    kicked_off_by_user_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )

    # Root Skrift Agent session for the lineage tree; each stage's session
    # is a child of this. Stored on the run row so the admin can replay /
    # subscribe via Skrift's event_log without a join.
    root_session_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )

    # Per-stage session ids populated as each stage completes:
    # {"review": "<uuid>", "scout": "<uuid>", ...}
    stage_session_ids: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )

    # FK to skrift.pages(id); declared raw in the migration. Set by the
    # Synthesis stage once the post lands.
    result_page_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_authoring_pipeline_runs_status_created",
            "status",
            "created_at",
        ),
    )


# ---------------------------------------------------------------------------
# Agentic handler system
# ---------------------------------------------------------------------------

# Allowed trigger families for a member-created handler.
HANDLER_TRIGGER_TYPES = ("message", "reaction", "schedule", "timer")
# Event triggers are single-listener per channel; time triggers coexist.
HANDLER_EVENT_TRIGGERS = ("message", "reaction")


class ChannelHandler(Base):
    """A member-created automation: a sandboxed script run when a trigger fires.

    ``id`` is the public ``handler_id`` surfaced by ``list_handlers`` and used by
    ``delete_handler``. ``name`` is the author-chosen label members and the
    authoring agent refer to a handler by — unique within a channel. Any number
    of handlers may share a (channel, trigger); every enabled one fires.
    """

    __tablename__ = "channel_handlers"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('message', 'reaction', 'schedule', 'timer')",
            name="ck_channel_handlers_trigger_type",
        ),
        Index("ix_channel_handlers_channel_id", "channel_id"),
        Index(
            "uq_channel_handlers_channel_name", "channel_id", "name", unique=True
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    guild_id: Mapped[str] = mapped_column(String(20), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    settings: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Persistent per-handler key/value store: the script reads/writes it via the
    # memory_* functions and it survives across fires (counters, seen-sets, etc.).
    memory: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # Queue job id for the next scheduled fire (time triggers), so delete can
    # cancel it.
    scheduled_job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class HandlerRun(Base):
    """Durable audit of a single handler firing, including budget spend.

    Written on every fire (ok, cap_exceeded, or error) so cost is auditable.
    """

    __tablename__ = "handler_runs"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('ok', 'cap_exceeded', 'error', 'rejected')",
            name="ck_handler_runs_outcome",
        ),
        Index("ix_handler_runs_handler_id", "handler_id"),
        # Serves the admin error log: a handler's recent runs by time.
        Index("ix_handler_runs_handler_id_fired_at", "handler_id", "fired_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    handler_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trigger_context: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # 'standard' (member ChannelHandler) or 'admin' (AdminHandler) — handler_id
    # references the matching table by kind.
    handler_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="standard", server_default="standard"
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    cap: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    web_searches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    web_reads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Moderation actions (ban/kick/timeout/delete) — admin handlers only.
    mod_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AdminHandler(Base):
    """Admin-only sandboxed handler with moderation powers.

    A separate tier from ``ChannelHandler``: created only via the admin slash
    command (author-written script, never editable by regular members). The
    script may call moderation external functions (ban/kick/timeout/delete) and
    send to any channel. ``channel_ids`` is the scope — empty/null means ALL
    channels in the guild; otherwise the listed channel ids. ``name`` is the
    author-chosen label admins and the authoring agent refer to a handler by —
    unique within a guild.
    """

    __tablename__ = "admin_handlers"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('message', 'reaction', 'schedule', 'timer')",
            name="ck_admin_handlers_trigger_type",
        ),
        Index("ix_admin_handlers_guild_id", "guild_id"),
        Index("uq_admin_handlers_guild_name", "guild_id", "name", unique=True),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    guild_id: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    settings: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # Scope: [] / null = all channels in the guild; else the listed channel ids.
    channel_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_admin: Mapped[str] = mapped_column(String(20), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Persistent per-handler key/value store (see ChannelHandler.memory).
    memory: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    scheduled_job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class MemberActivity(Base):
    """Per-(guild, member) message activity: first and most recent message times.

    Fed by the bot's batched activity reports (all human guild messages) and
    updated synchronously at handler dispatch. Handler trigger contexts derive
    "first message ever" / "first message in N days" facts from this, so
    scripts never track per-user history in their size-capped memory.
    """

    __tablename__ = "member_activity"
    __table_args__ = (
        Index("uq_member_activity_guild_user", "guild_id", "user_id", unique=True),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    guild_id: Mapped[str] = mapped_column(String(20), nullable=False)
    user_id: Mapped[str] = mapped_column(String(20), nullable=False)
    first_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ChannelModelOverride(Base):
    """An admin-set LLM model override + token budgets for a single channel.

    Set via the admin ``/model`` slash command: it pins which catalog model the
    chat agent uses in ``channel_id`` and caps spend with daily/hourly token
    budgets (``0`` means unlimited; enforcement lives in the chat runtime, not
    here). Exactly one row per channel — the slash command's PUT is an upsert, so
    reopening the modal edits the existing row rather than stacking duplicates.

    ``model_key`` is a stable ``key`` from
    :data:`smarter_dev.shared.model_catalog.MODEL_CATALOG`; the wire
    ``model_id`` is resolved from it at request time, so re-verifying a model's
    wire id never needs a migration here.
    """

    __tablename__ = "channel_model_overrides"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", name="uq_channel_model_overrides_channel_id"
        ),
        Index("ix_channel_model_overrides_guild_id", "guild_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    guild_id: Mapped[str] = mapped_column(String(20), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(20), nullable=False)
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # A ReasoningLevel value (e.g. "high"), or NULL to use the model's default.
    # Resolved/clamped against the selected model at request time, so a level the
    # model no longer supports never needs a migration here.
    reasoning_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 0 == unlimited for both budgets (enforced in the chat runtime).
    daily_token_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    hourly_token_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # When true the chat bot activates on ANY channel message, not just @mentions.
    auto_respond: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A stable catalog ``key`` to fall back to when the primary model is
    # unavailable, or NULL to use no fallback.
    fallback_model_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Free-text instructions describing which messages deserve a response, or NULL
    # to respond without a content filter.
    response_filter: Mapped[str | None] = mapped_column(Text, nullable=True)
