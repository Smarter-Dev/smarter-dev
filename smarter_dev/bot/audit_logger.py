"""Audit logger service for Discord events.

This module provides functionality to log Discord events (member join/leave,
bans, message edits/deletes, etc.) to a configured audit log channel with
formatted embeds.
"""

from __future__ import annotations

import difflib
import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import hikari

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import AuditLogConfigOperations

logger = logging.getLogger(__name__)

# Color scheme for different event types
COLORS = {
    "member_join": hikari.Color.from_rgb(88, 211, 96),  # Green
    "member_leave": hikari.Color.from_rgb(247, 93, 93),  # Red
    "member_ban": hikari.Color.from_rgb(237, 66, 69),  # Dark red
    "member_unban": hikari.Color.from_rgb(91, 196, 251),  # Light blue
    "message_edit": hikari.Color.from_rgb(254, 231, 92),  # Yellow
    "message_delete": hikari.Color.from_rgb(250, 166, 26),  # Orange
    "username_change": hikari.Color.from_rgb(155, 105, 245),  # Purple
    "nickname_change": hikari.Color.from_rgb(206, 111, 245),  # Light purple
    "role_change": hikari.Color.from_rgb(102, 153, 255),  # Blue
}


def format_diff(old_text: str, new_text: str, max_length: int = 1024) -> str:
    """Format a diff between two texts in Discord markdown.

    Args:
        old_text: Original text
        new_text: New text
        max_length: Maximum length of the diff (default 1024 for embed field)

    Returns:
        Formatted diff string with + and - prefixes
    """
    # Split into lines
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    # Generate diff with 2 lines of context
    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        lineterm='',
        n=2  # 2 context lines around changes
    ))

    # Skip the header lines (---, +++, @@)
    diff_lines = []
    for line in diff[3:]:  # Skip first 3 header lines
        if line.startswith('+'):
            diff_lines.append(f"+ {line[1:]}")
        elif line.startswith('-'):
            diff_lines.append(f"- {line[1:]}")
        elif line.startswith('@@'):
            # Skip hunk headers
            continue
        else:
            # Context line (unchanged)
            diff_lines.append(f"  {line[1:]}" if line.startswith(' ') else f"  {line}")

    # If no meaningful diff, show before/after
    if not diff_lines:
        diff_text = f"**Before:**\n{old_text}\n\n**After:**\n{new_text}"
    else:
        diff_text = "\n".join(diff_lines)

    # Truncate if too long
    if len(diff_text) > max_length:
        diff_text = diff_text[:max_length - 3] + "..."

    return diff_text


async def send_audit_log(
    bot: hikari.GatewayBot,
    guild_id: int,
    embed: hikari.Embed
) -> bool:
    """Send an audit log embed to the configured channel.

    Args:
        bot: Discord bot instance
        guild_id: Guild ID
        embed: Embed to send

    Returns:
        True if sent successfully, False otherwise
    """
    try:
        # Get audit log configuration from database
        async with get_db_session_context() as session:
            audit_ops = AuditLogConfigOperations()
            config = await audit_ops.get_config(session, str(guild_id))

            if not config or not config.audit_channel_id:
                # No audit log configured
                return False

            # Send the embed to the audit channel
            channel_id = int(config.audit_channel_id)
            await bot.rest.create_message(channel_id, embed=embed)
            return True

    except hikari.ForbiddenError:
        logger.warning(f"Missing permissions to send audit log in guild {guild_id}")
        return False
    except hikari.NotFoundError:
        logger.warning(f"Audit log channel not found for guild {guild_id}")
        return False
    except Exception as e:
        logger.error(f"Failed to send audit log for guild {guild_id}: {e}")
        return False


async def should_log_event(guild_id: int, event_type: str) -> bool:
    """Check if an event type should be logged for a guild.

    Args:
        guild_id: Guild ID
        event_type: Event type field name (e.g., 'log_member_join')

    Returns:
        True if the event should be logged, False otherwise
    """
    try:
        async with get_db_session_context() as session:
            audit_ops = AuditLogConfigOperations()
            config = await audit_ops.get_config(session, str(guild_id))

            if not config or not config.audit_channel_id:
                return False

            return getattr(config, event_type, False)

    except Exception as e:
        logger.error(f"Failed to check audit log config for guild {guild_id}: {e}")
        return False


# Event-specific embed builders

async def log_member_join(
    bot: hikari.GatewayBot,
    event: hikari.MemberCreateEvent
) -> None:
    """Log a member join event.

    Args:
        bot: Discord bot instance
        event: Member create event
    """
    if not await should_log_event(event.guild_id, "log_member_join"):
        return

    member = event.member
    embed = hikari.Embed(
        title="Member Joined",
        description=f"{member.mention} {member.username}",
        color=COLORS["member_join"],
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User ID", value=str(member.id), inline=True)
    embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)

    if member.avatar_url:
        embed.set_thumbnail(member.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)


async def log_member_leave(
    bot: hikari.GatewayBot,
    event: hikari.MemberDeleteEvent
) -> None:
    """Log a member leave event.

    Args:
        bot: Discord bot instance
        event: Member delete event
    """
    if not await should_log_event(event.guild_id, "log_member_leave"):
        return

    user = event.user
    embed = hikari.Embed(
        title="Member Left",
        description=f"{user.mention} {user.username}",
        color=COLORS["member_leave"],
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User ID", value=str(user.id), inline=True)

    if user.avatar_url:
        embed.set_thumbnail(user.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)


async def log_member_ban(
    bot: hikari.GatewayBot,
    event: hikari.BanCreateEvent
) -> None:
    """Log a member ban event.

    Args:
        bot: Discord bot instance
        event: Ban create event
    """
    if not await should_log_event(event.guild_id, "log_member_ban"):
        return

    user = event.user
    embed = hikari.Embed(
        title="Member Banned",
        description=f"{user.mention} {user.username}",
        color=COLORS["member_ban"],
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User ID", value=str(user.id), inline=True)

    if user.avatar_url:
        embed.set_thumbnail(user.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)


async def log_member_unban(
    bot: hikari.GatewayBot,
    event: hikari.BanDeleteEvent
) -> None:
    """Log a member unban event.

    Args:
        bot: Discord bot instance
        event: Ban delete event
    """
    if not await should_log_event(event.guild_id, "log_member_unban"):
        return

    user = event.user
    embed = hikari.Embed(
        title="Member Unbanned",
        description=f"{user.mention} {user.username}",
        color=COLORS["member_unban"],
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User ID", value=str(user.id), inline=True)

    if user.avatar_url:
        embed.set_thumbnail(user.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)


async def log_message_edit(
    bot: hikari.GatewayBot,
    event: hikari.GuildMessageUpdateEvent
) -> None:
    """Log a message edit event with diff.

    Args:
        bot: Discord bot instance
        event: Message update event
    """
    if not await should_log_event(event.guild_id, "log_message_edit"):
        return

    # Get author info
    author = event.message.author
    if not author:
        return

    # Check if we have the old message (cached)
    if event.old_message is not None:
        # Skip if no content change (e.g., embed update)
        if event.old_message.content == event.message.content:
            return

        # Create diff showing before/after
        diff = format_diff(event.old_message.content or "", event.message.content or "")
        changes_value = f"```diff\n{diff}\n```"
    else:
        # Message wasn't cached - show current content only
        current_content = event.message.content or ""
        if len(current_content) > 1000:
            current_content = current_content[:997] + "..."
        changes_value = f"**New Content:**\n{current_content}\n\n*Original content not available (message not cached)*"

    embed = hikari.Embed(
        title="Message Edited",
        description=f"Message by {author.mention} edited in <#{event.channel_id}>",
        color=COLORS["message_edit"],
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Author", value=f"{author.username} ({author.id})", inline=True)
    embed.add_field(name="Channel", value=f"<#{event.channel_id}>", inline=True)
    embed.add_field(name="Message ID", value=str(event.message.id), inline=True)
    embed.add_field(name="Changes", value=changes_value, inline=False)
    embed.add_field(name="Jump to Message", value=f"[Click here](https://discord.com/channels/{event.guild_id}/{event.channel_id}/{event.message.id})", inline=False)

    if author.avatar_url:
        embed.set_thumbnail(author.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)


async def log_message_delete(
    bot: hikari.GatewayBot,
    event: hikari.GuildMessageDeleteEvent
) -> None:
    """Log a message delete event.

    Args:
        bot: Discord bot instance
        event: Message delete event
    """
    if not await should_log_event(event.guild_id, "log_message_delete"):
        return

    # Get message from event (if cached)
    old_message = event.old_message

    embed = hikari.Embed(
        title="Message Deleted",
        description=f"Message deleted in <#{event.channel_id}>",
        color=COLORS["message_delete"],
        timestamp=datetime.now(timezone.utc)
    )

    if old_message:
        author = old_message.author
        embed.add_field(name="Author", value=f"{author.mention} ({author.username})", inline=True)
        embed.add_field(name="Author ID", value=str(author.id), inline=True)

        if old_message.content:
            content = old_message.content
            if len(content) > 1024:
                content = content[:1021] + "..."
            embed.add_field(name="Content", value=content, inline=False)

        if author.avatar_url:
            embed.set_thumbnail(author.avatar_url)
    else:
        embed.add_field(name="Note", value="Message was not cached, author information unavailable", inline=False)

    embed.add_field(name="Channel", value=f"<#{event.channel_id}>", inline=True)
    embed.add_field(name="Message ID", value=str(event.message_id), inline=True)

    await send_audit_log(bot, event.guild_id, embed)


async def log_member_update(
    bot: hikari.GatewayBot,
    event: hikari.MemberUpdateEvent
) -> None:
    """Log member update events (username, nickname, role changes).

    Args:
        bot: Discord bot instance
        event: Member update event
    """
    if not event.old_member:
        return

    member = event.member
    old_member = event.old_member

    # Check for username change
    if old_member.username != member.username:
        if await should_log_event(event.guild_id, "log_username_change"):
            embed = hikari.Embed(
                title="Username Changed",
                description=f"{member.mention} changed their username",
                color=COLORS["username_change"],
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Old Username", value=old_member.username, inline=True)
            embed.add_field(name="New Username", value=member.username, inline=True)
            embed.add_field(name="User ID", value=str(member.id), inline=True)

            if member.avatar_url:
                embed.set_thumbnail(member.avatar_url)

            await send_audit_log(bot, event.guild_id, embed)

    # Check for nickname change
    if old_member.nickname != member.nickname:
        if await should_log_event(event.guild_id, "log_nickname_change"):
            embed = hikari.Embed(
                title="Nickname Changed",
                description=f"{member.mention} changed their nickname",
                color=COLORS["nickname_change"],
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Username", value=member.username, inline=True)
            embed.add_field(name="Old Nickname", value=old_member.nickname or "None", inline=True)
            embed.add_field(name="New Nickname", value=member.nickname or "None", inline=True)
            embed.add_field(name="User ID", value=str(member.id), inline=True)

            if member.avatar_url:
                embed.set_thumbnail(member.avatar_url)

            await send_audit_log(bot, event.guild_id, embed)

    # Check for role changes
    if old_member.role_ids != member.role_ids:
        if await should_log_event(event.guild_id, "log_role_change"):
            added_roles = set(member.role_ids) - set(old_member.role_ids)
            removed_roles = set(old_member.role_ids) - set(member.role_ids)

            embed = hikari.Embed(
                title="Member Roles Changed",
                description=f"{member.mention} {member.username}",
                color=COLORS["role_change"],
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User ID", value=str(member.id), inline=True)

            if added_roles:
                roles_str = " ".join([f"<@&{role_id}>" for role_id in added_roles])
                embed.add_field(name="Roles Added", value=roles_str, inline=False)

            if removed_roles:
                roles_str = " ".join([f"<@&{role_id}>" for role_id in removed_roles])
                embed.add_field(name="Roles Removed", value=roles_str, inline=False)

            if member.avatar_url:
                embed.set_thumbnail(member.avatar_url)

            await send_audit_log(bot, event.guild_id, embed)
