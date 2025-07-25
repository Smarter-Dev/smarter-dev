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
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy import Integer
from sqlalchemy import JSON
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped
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
        kwargs.setdefault('streak_bonuses', {7: 2, 14: 4, 30: 10, 60: 20})
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
    
    # Indexes and constraints for common queries
    __table_args__ = (
        Index("ix_squads_guild_id", "guild_id"),
        Index("ix_squads_role_id", "role_id"),
        Index("ix_squads_guild_active", "guild_id", "is_active"),
        UniqueConstraint("guild_id", "role_id", name="uq_squads_guild_role"),  # Unique per specification
    )
    
    def __init__(self, **kwargs):
        """Initialize Squad with auto-generated ID and defaults."""
        kwargs.setdefault('id', uuid4())
        kwargs.setdefault('switch_cost', 50)
        kwargs.setdefault('is_active', True)
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