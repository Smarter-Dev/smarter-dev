"""
Data models for the Smarter Dev API.

This module contains dataclasses that represent the data models used by the API.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Union
from datetime import datetime


@dataclass
class Guild:
    """Discord Guild model"""
    id: int
    discord_id: int
    name: str
    icon_url: Optional[str] = None
    joined_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


@dataclass
class DiscordUser:
    """Discord User model"""
    id: int
    discord_id: int
    username: str
    discriminator: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: Optional[datetime] = None
    bytes_balance: int = 0


@dataclass
class GuildMember:
    """Guild Member model"""
    id: int
    user_id: int
    guild_id: int
    nickname: Optional[str] = None
    joined_at: Optional[datetime] = None
    is_active: bool = True
    created_at: Optional[datetime] = None



@dataclass
class UserNote:
    """User Note model"""
    id: Optional[int] = None  # Optional for creation
    user_id: int = 0
    mod_id: int = 0
    guild_id: int = 0
    content: str = ""
    noted_at: Optional[datetime] = None


@dataclass
class UserWarning:
    """User Warning model"""
    id: Optional[int] = None  # Optional for creation
    user_id: int = 0
    mod_id: int = 0
    guild_id: int = 0
    reason: Optional[str] = None
    warned_at: Optional[datetime] = None


@dataclass
class ModerationCase:
    """Moderation Case model"""
    id: Optional[int] = None  # Optional for creation
    case_number: Optional[int] = None  # Optional for creation
    guild_id: int = 0
    user_id: int = 0
    mod_id: int = 0
    action: str = ""
    reason: Optional[str] = None
    created_at: Optional[datetime] = None
    duration_sec: Optional[int] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None


@dataclass
class PersistentRole:
    """Persistent Role model"""
    id: Optional[int] = None  # Optional for creation
    user_id: int = 0
    guild_id: int = 0
    role_id: int = 0
    role_name: Optional[str] = None
    assigned_at: Optional[datetime] = None


@dataclass
class TemporaryRole:
    """Temporary Role model"""
    id: Optional[int] = None  # Optional for creation
    user_id: int = 0
    guild_id: int = 0
    role_id: int = 0
    role_name: Optional[str] = None
    assigned_at: Optional[datetime] = None
    expires_at: datetime = field(default_factory=datetime.now)
    reason: Optional[str] = None


@dataclass
class ChannelLock:
    """Channel Lock model"""
    id: Optional[int] = None  # Optional for creation
    guild_id: int = 0
    channel_id: int = 0
    channel_name: Optional[str] = None
    locked_by: Optional[int] = None
    locked_at: Optional[datetime] = None
    unlock_at: Optional[datetime] = None
    message: Optional[str] = None


@dataclass
class BumpStat:
    """Bump Stat model"""
    id: Optional[int] = None  # Optional for creation
    user_id: Optional[int] = None
    guild_id: Optional[int] = None
    bump_count: int = 0
    last_bumped_at: Optional[datetime] = None


@dataclass
class CommandUsage:
    """Command Usage model"""
    id: Optional[int] = None  # Optional for creation
    user_id: Optional[int] = None
    guild_id: Optional[int] = None
    command_name: str = ""
    usage_count: int = 1
    last_used_at: Optional[datetime] = None


@dataclass
class Bytes:
    """Bytes model (renamed from Kudos)"""
    id: Optional[int] = None  # Optional for creation
    giver_id: int = 0
    receiver_id: int = 0
    guild_id: int = 0
    amount: int = 1
    reason: Optional[str] = None
    awarded_at: Optional[datetime] = None


@dataclass
class BytesConfig:
    """Bytes Configuration model"""
    id: Optional[int] = None  # Optional for creation
    guild_id: int = 0
    starting_balance: int = 100
    daily_earning: int = 10
    max_give_amount: int = 50
    cooldown_minutes: int = 1440  # Default: 24 hours
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class BytesRole:
    """Bytes Role Rewards model"""
    id: Optional[int] = None  # Optional for creation
    guild_id: int = 0
    role_id: int = 0
    role_name: str = ""
    bytes_required: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class BytesCooldown:
    """Bytes Cooldown tracking model"""
    id: Optional[int] = None  # Optional for creation
    user_id: int = 0
    guild_id: int = 0
    last_given_at: Optional[datetime] = None
