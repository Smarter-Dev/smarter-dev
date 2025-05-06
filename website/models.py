from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, BigInteger, func
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import datetime

from .database import Base

# This is a placeholder model for future use
class Subscriber(Base):
    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<Subscriber {self.email}>"

# Admin user model
class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<AdminUser {self.username}>"

# Redirect model
class Redirect(Base):
    __tablename__ = "redirects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)  # URL-safe name for the redirect
    target_url = Column(String)  # URL to redirect to
    description = Column(Text, nullable=True)  # Optional description
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationship with clicks
    clicks = relationship("RedirectClick", back_populates="redirect", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Redirect {self.name} -> {self.target_url}>"

# Redirect click tracking model
class RedirectClick(Base):
    __tablename__ = "redirect_clicks"

    id = Column(Integer, primary_key=True, index=True)
    redirect_id = Column(Integer, ForeignKey("redirects.id"))
    ip_address = Column(String, nullable=True)  # Store IP address (consider privacy implications)
    user_agent = Column(String, nullable=True)  # Store user agent
    referer = Column(String, nullable=True)  # Store referer URL
    timestamp = Column(DateTime, default=func.now())

    # Relationship with redirect
    redirect = relationship("Redirect", back_populates="clicks")

    def __repr__(self):
        return f"<RedirectClick for {self.redirect_id} at {self.timestamp}>"

# Page view tracking model
class PageView(Base):
    __tablename__ = "page_views"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String)  # URL path
    method = Column(String)  # HTTP method (GET, POST, etc.)
    ip_address = Column(String, nullable=True)  # Store IP address (consider privacy implications)
    user_agent = Column(String, nullable=True)  # Store user agent
    referer = Column(String, nullable=True)  # Store referer URL
    response_time = Column(Float, nullable=True)  # Response time in seconds
    status_code = Column(Integer, nullable=True)  # HTTP status code
    is_bot = Column(Boolean, default=False)  # Flag to indicate if the view is from a bot
    timestamp = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<PageView {self.path} at {self.timestamp}>"

# Route error tracking model
class RouteError(Base):
    __tablename__ = "route_errors"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String)  # URL path
    method = Column(String)  # HTTP method (GET, POST, etc.)
    ip_address = Column(String, nullable=True)  # Store IP address
    user_agent = Column(String, nullable=True)  # Store user agent
    error_type = Column(String)  # Exception type
    error_message = Column(Text)  # Exception message
    error_details = Column(Text, nullable=True)  # Full traceback
    response_time = Column(Float, nullable=True)  # Response time in seconds
    timestamp = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<RouteError {self.error_type} at {self.path}>"


# API Key model for Discord bot authentication
class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    name = Column(String, nullable=False)  # A descriptive name for the key
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    last_used_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<APIKey {self.name}>"


# Discord Guild model
class Guild(Base):
    __tablename__ = "guilds"

    id = Column(Integer, primary_key=True, index=True)
    discord_id = Column(BigInteger, unique=True, nullable=False)
    name = Column(String, nullable=False)
    icon_url = Column(String, nullable=True)
    joined_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    # Relationships
    users = relationship("GuildMember", back_populates="guild")
    moderation_cases = relationship("ModerationCase", back_populates="guild")
    channel_locks = relationship("ChannelLock", back_populates="guild")
    bump_stats = relationship("BumpStat", back_populates="guild")
    command_usage = relationship("CommandUsage", back_populates="guild")
    bytes_config = relationship("BytesConfig", back_populates="guild", uselist=False)
    bytes_roles = relationship("BytesRole", back_populates="guild")

    def __repr__(self):
        return f"<Guild {self.name} ({self.discord_id})>"


# Discord User model
class DiscordUser(Base):
    __tablename__ = "discord_users"

    id = Column(Integer, primary_key=True, index=True)
    discord_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String, nullable=False)
    discriminator = Column(String, nullable=True)  # May be null for newer Discord accounts
    avatar_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    bytes_balance = Column(Integer, default=0, nullable=False)  # Current bytes balance

    # Relationships
    guild_memberships = relationship("GuildMember", back_populates="user")
    given_bytes = relationship("Bytes", foreign_keys="Bytes.giver_id", back_populates="giver")
    received_bytes = relationship("Bytes", foreign_keys="Bytes.receiver_id", back_populates="receiver")
    notes = relationship("UserNote", foreign_keys="UserNote.user_id", back_populates="user")
    warnings = relationship("UserWarning", foreign_keys="UserWarning.user_id", back_populates="user")
    moderation_cases = relationship("ModerationCase", foreign_keys="ModerationCase.user_id", back_populates="user")
    mod_actions = relationship("ModerationCase", foreign_keys="ModerationCase.mod_id", back_populates="moderator")
    persistent_roles = relationship("PersistentRole", back_populates="user")
    temporary_roles = relationship("TemporaryRole", back_populates="user")
    bump_stats = relationship("BumpStat", back_populates="user")
    command_usage = relationship("CommandUsage", back_populates="user")
    bytes_cooldowns = relationship("BytesCooldown", back_populates="user")

    def __repr__(self):
        return f"<DiscordUser {self.username} ({self.discord_id})>"


# Guild Member model (join table with additional data)
class GuildMember(Base):
    __tablename__ = "guild_members"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    nickname = Column(String, nullable=True)
    joined_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)  # False if user has left the guild
    created_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("DiscordUser", back_populates="guild_memberships")
    guild = relationship("Guild", back_populates="users")

    def __repr__(self):
        return f"<GuildMember {self.user_id} in {self.guild_id}>"



# User Notes model
class UserNote(Base):
    __tablename__ = "user_notes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    mod_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    content = Column(Text, nullable=False)
    noted_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("DiscordUser", foreign_keys=[user_id], back_populates="notes")
    moderator = relationship("DiscordUser", foreign_keys=[mod_id], overlaps="notes,warnings,moderation_cases,mod_actions")
    guild = relationship("Guild")

    def __repr__(self):
        return f"<UserNote for {self.user_id} by {self.mod_id}>"


# User Warnings model
class UserWarning(Base):
    __tablename__ = "user_warnings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    mod_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    reason = Column(Text, nullable=True)
    warned_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("DiscordUser", foreign_keys=[user_id], back_populates="warnings")
    moderator = relationship("DiscordUser", foreign_keys=[mod_id], overlaps="notes,warnings,moderation_cases,mod_actions")
    guild = relationship("Guild")

    def __repr__(self):
        return f"<UserWarning for {self.user_id} by {self.mod_id}>"


# Moderation Cases model
class ModerationCase(Base):
    __tablename__ = "moderation_cases"

    id = Column(Integer, primary_key=True, index=True)
    case_number = Column(Integer, nullable=False)  # Sequential case number per guild
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    mod_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    action = Column(String, nullable=False)  # e.g., 'ban', 'mute', 'softban'
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    duration_sec = Column(Integer, nullable=True)  # For temporary actions
    resolved_at = Column(DateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)

    # Relationships
    guild = relationship("Guild", back_populates="moderation_cases")
    user = relationship("DiscordUser", foreign_keys=[user_id], back_populates="moderation_cases")
    moderator = relationship("DiscordUser", foreign_keys=[mod_id], back_populates="mod_actions", overlaps="notes,warnings,moderation_cases,mod_actions")

    def __repr__(self):
        return f"<ModerationCase #{self.case_number} in {self.guild_id}>"


# Persistent Roles model
class PersistentRole(Base):
    __tablename__ = "persistent_roles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    role_id = Column(BigInteger, nullable=False)
    role_name = Column(String, nullable=True)  # Store role name for reference
    assigned_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("DiscordUser", back_populates="persistent_roles")
    guild = relationship("Guild")

    def __repr__(self):
        return f"<PersistentRole {self.role_id} for {self.user_id}>"


# Temporary Roles model
class TemporaryRole(Base):
    __tablename__ = "temporary_roles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    role_id = Column(BigInteger, nullable=False)
    role_name = Column(String, nullable=True)  # Store role name for reference
    assigned_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=False)
    reason = Column(Text, nullable=True)

    # Relationships
    user = relationship("DiscordUser", back_populates="temporary_roles")
    guild = relationship("Guild")

    def __repr__(self):
        return f"<TemporaryRole {self.role_id} for {self.user_id} until {self.expires_at}>"


# Channel Locks model
class ChannelLock(Base):
    __tablename__ = "channel_locks"

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    channel_id = Column(BigInteger, nullable=False)
    channel_name = Column(String, nullable=True)  # Store channel name for reference
    locked_by = Column(Integer, ForeignKey("discord_users.id"), nullable=True)
    locked_at = Column(DateTime, default=func.now())
    unlock_at = Column(DateTime, nullable=True)
    message = Column(Text, nullable=True)

    # Relationships
    guild = relationship("Guild", back_populates="channel_locks")
    moderator = relationship("DiscordUser", foreign_keys=[locked_by])

    def __repr__(self):
        return f"<ChannelLock for {self.channel_id} in {self.guild_id}>"


# Bump Stats model
class BumpStat(Base):
    __tablename__ = "bump_stats"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=True)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=True)
    bump_count = Column(Integer, default=0, nullable=False)
    last_bumped_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("DiscordUser", back_populates="bump_stats")
    guild = relationship("Guild", back_populates="bump_stats")

    def __repr__(self):
        return f"<BumpStat for {self.user_id} in {self.guild_id}: {self.bump_count}>"


# Command Usage model
class CommandUsage(Base):
    __tablename__ = "command_usage"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=True)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=True)
    command_name = Column(String, nullable=False)
    usage_count = Column(Integer, default=1, nullable=False)
    last_used_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("DiscordUser", back_populates="command_usage")
    guild = relationship("Guild", back_populates="command_usage")

    def __repr__(self):
        return f"<CommandUsage {self.command_name} by {self.user_id}: {self.usage_count}>"


# Bytes model (renamed from Kudos)
class Bytes(Base):
    __tablename__ = "bytes"

    id = Column(Integer, primary_key=True, index=True)
    giver_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    amount = Column(Integer, default=1, nullable=False)
    reason = Column(Text, nullable=True)
    awarded_at = Column(DateTime, default=func.now())

    # Relationships
    giver = relationship("DiscordUser", foreign_keys=[giver_id], back_populates="given_bytes")
    receiver = relationship("DiscordUser", foreign_keys=[receiver_id], back_populates="received_bytes")
    guild = relationship("Guild")

    def __repr__(self):
        return f"<Bytes from {self.giver_id} to {self.receiver_id}>"


# Bytes Configuration model
class BytesConfig(Base):
    __tablename__ = "bytes_config"

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(Integer, ForeignKey("guilds.id"), unique=True, nullable=False)
    starting_balance = Column(Integer, default=100, nullable=False)  # Default bytes for new users
    daily_earning = Column(Integer, default=10, nullable=False)  # Daily bytes earned for activity
    max_give_amount = Column(Integer, default=50, nullable=False)  # Maximum bytes a user can give at once
    cooldown_minutes = Column(Integer, default=1440, nullable=False)  # Default: 24 hours (1440 minutes)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    guild = relationship("Guild", back_populates="bytes_config")

    def __repr__(self):
        return f"<BytesConfig for guild {self.guild_id}>"


# Bytes Role Rewards model
class BytesRole(Base):
    __tablename__ = "bytes_roles"

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    role_id = Column(BigInteger, nullable=False)
    role_name = Column(String, nullable=False)
    bytes_required = Column(Integer, nullable=False)  # Bytes required to earn this role
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    guild = relationship("Guild", back_populates="bytes_roles")

    def __repr__(self):
        return f"<BytesRole {self.role_name} ({self.bytes_required} bytes)>"


# Bytes Cooldown tracking model
class BytesCooldown(Base):
    __tablename__ = "bytes_cooldowns"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("discord_users.id"), nullable=False)
    guild_id = Column(Integer, ForeignKey("guilds.id"), nullable=False)
    last_given_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("DiscordUser", back_populates="bytes_cooldowns")
    guild = relationship("Guild")

    def __repr__(self):
        return f"<BytesCooldown for user {self.user_id} in guild {self.guild_id}>"

