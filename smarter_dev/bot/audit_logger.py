"""Audit logger service for Discord events.

This module provides functionality to log Discord events (member join/leave,
bans, message edits/deletes, etc.) to a configured audit log channel with
formatted embeds.

It also ingests native moderation actions (ban, unban, kick, timeout,
untimeout) into ``moderation_actions``. An action somebody else took keeps its
actor and its Discord audit-log entry id. An action the bot took itself is the
echo of a moderation-gateway operation, which already wrote its row: the entry
is linked to that operation (``link_audit_entry``), never recorded twice.
"""

from __future__ import annotations

import difflib
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import hikari

from smarter_dev.bot.mod_action_dispatch import dispatch_mod_action
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import AuditLogConfigOperations, ModerationActionOperations
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.moderation_gateway.gateway import link_audit_entry

mod_action_ops = ModerationActionOperations()

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

#: Discord refuses (400) an embed whose field value is longer than this.
FIELD_VALUE_LIMIT = 1024

DIFF_OPEN = "```diff\n"
DIFF_CLOSE = "\n```"
UNCACHED_OPEN = "**New Content:**\n"
UNCACHED_CLOSE = "\n\n*Original content not available (message not cached)*"


#: How many recent bot and webhook message ids are remembered, so a delete of
#: one that left hikari's message cache is still recognised and skipped.
AUTOMATED_MESSAGE_MEMORY = 10_000

_automated_message_ids: OrderedDict[int, None] = OrderedDict()


def is_automated(message) -> bool:
    """Whether ``message`` was posted by a bot account or a webhook.

    The activity log is for what members do; Disboard, the bump tracker, our
    own notices and webhook posts are noise there. Fields hikari leaves
    UNDEFINED on a partial message are falsy, so they read as "not automated".
    """
    author = getattr(message, "author", None)
    return bool(getattr(message, "webhook_id", None)) or bool(
        author and getattr(author, "is_bot", False)
    )


def remember_message(message) -> None:
    """Note ``message``'s id when it is automated; called on message create."""
    if not is_automated(message):
        return
    _automated_message_ids[int(message.id)] = None
    _automated_message_ids.move_to_end(int(message.id))
    while len(_automated_message_ids) > AUTOMATED_MESSAGE_MEMORY:
        _automated_message_ids.popitem(last=False)


def _was_automated(message_id, message) -> bool:
    """``message`` (when known) is automated, or its id was remembered as such."""
    if message is not None and is_automated(message):
        return True
    return int(message_id) in _automated_message_ids


def clip(text: str, limit: int = FIELD_VALUE_LIMIT) -> str:
    """``text`` cut to ``limit`` characters, ending in "..." when cut."""
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def join_within(items: Sequence[str], limit: int = FIELD_VALUE_LIMIT) -> str:
    """``items`` space-joined, whole items only, with "and N more" when cut."""
    whole = " ".join(items)
    if len(whole) <= limit:
        return whole
    shown: list[str] = []
    for index, item in enumerate(items):
        if len(" ".join([*shown, item, f"and {len(items) - index - 1} more"])) > limit:
            break
        shown.append(item)
    return " ".join([*shown, f"and {len(items) - len(shown)} more"])


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
        lineterm="",
        n=2  # 2 context lines around changes
    ))

    # Skip the header lines (---, +++, @@)
    diff_lines = []
    for line in diff[3:]:  # Skip first 3 header lines
        if line.startswith("+"):
            diff_lines.append(f"+ {line[1:]}")
        elif line.startswith("-"):
            diff_lines.append(f"- {line[1:]}")
        elif line.startswith("@@"):
            # Skip hunk headers
            continue
        else:
            # Context line (unchanged)
            diff_lines.append(f"  {line[1:]}" if line.startswith(" ") else f"  {line}")

    # If no meaningful diff, show before/after
    if not diff_lines:
        diff_text = f"**Before:**\n{old_text}\n\n**After:**\n{new_text}"
    else:
        diff_text = "\n".join(diff_lines)

    return clip(diff_text, max_length)


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
        logger.error(f"Failed to send audit log for guild {guild_id}: {type(e).__name__}")
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
        logger.error(f"Failed to check audit log config for guild {guild_id}: {type(e).__name__}")
        return False


def _entry_matches_change(entry: hikari.AuditLogEntry, change_key) -> bool:
    """Whether an audit-log entry carries the given change key.

    ``None`` means "no change-key requirement". Used to distinguish a genuine
    timeout add/clear (COMMUNICATION_DISABLED_UNTIL) from the many other things a
    MEMBER_UPDATE entry can represent (nickname, deaf, mute changes).
    """
    if change_key is None:
        return True
    changes = getattr(entry, "changes", None) or []
    return any(getattr(change, "key", None) == change_key for change in changes)


@dataclass(frozen=True, slots=True)
class AuditAttribution:
    """Who took a native action, from the guild audit log (someone other than the bot)."""

    moderator_id: str | None
    moderator_name: str | None
    reason: str | None
    entry_id: str | None


async def _lookup_audit_moderator(
    bot: hikari.GatewayBot,
    guild_id: int,
    event_type: hikari.AuditLogEventType,
    target_id: int,
    *,
    own_kinds: Sequence[str] = (),
    change_key=None,
    max_age_seconds: float | None = None,
) -> AuditAttribution | None:
    """Find the moderator behind a native action from the guild audit log.

    ``fetch_audit_log`` yields ``AuditLog`` *pages* (each a sequence of
    ``AuditLogEntry``), newest first, so we iterate the entries of the single
    fetched page and pick the newest one that targets ``target_id`` and, when
    required, carries ``change_key``. Returns None when there is no attributable
    entry — the entry is missing, older than ``max_age_seconds``, lacks
    ``change_key``, or was performed by the bot itself. An entry by the bot is
    linked to the gateway operation of one of ``own_kinds`` it echoes, which
    already wrote its row. A REST failure is swallowed so a moderation event is
    never lost to a hiccup.
    """
    me = bot.get_me()
    bot_id = me.id if me is not None else None
    try:
        async for page in bot.rest.fetch_audit_log(
            guild_id, event_type=event_type
        ).limit(1):
            for entry in page:
                if str(entry.target_id) != str(target_id):
                    continue
                if not _entry_matches_change(entry, change_key):
                    continue
                if max_age_seconds is not None:
                    created_at = getattr(entry.id, "created_at", None)
                    entry_time = created_at.timestamp() if created_at else 0
                    if abs(time.time() - entry_time) > max_age_seconds:
                        return None
                if entry.user_id and bot_id is not None and entry.user_id == bot_id:
                    if own_kinds:
                        await _link_own_action(guild_id, target_id, own_kinds, entry.id)
                    return None
                moderator_id = str(entry.user_id) if entry.user_id else None
                actor = (
                    await bot.rest.fetch_user(entry.user_id)
                    if entry.user_id
                    else None
                )
                return AuditAttribution(
                    moderator_id=moderator_id,
                    moderator_name=actor.username if actor else None,
                    reason=entry.reason,
                    entry_id=str(entry.id),
                )
    except Exception:
        log_exception(
            logger,
            "Could not fetch audit log for %s in guild %s",
            event_type,
            guild_id,
            level=logging.DEBUG,
        )
    return None


def _unpack(
    attribution: AuditAttribution | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if attribution is None:
        return None, None, None, None
    return (
        attribution.moderator_id,
        attribution.moderator_name,
        attribution.reason,
        attribution.entry_id,
    )


async def _link_own_action(
    guild_id: int, target_id: int, kinds: Sequence[str], entry_id
) -> None:
    try:
        linked = await link_audit_entry(
            guild_id=str(guild_id),
            target_user_id=str(target_id),
            kinds=kinds,
            audit_entry_id=str(entry_id),
        )
    except Exception:  # noqa: BLE001 — a missing link loses nothing but the id
        log_exception(
            logger,
            "Could not link audit entry %s to its moderation operation",
            entry_id,
            level=logging.WARNING,
        )
        return
    if not linked:
        logger.info(
            "Audit entry %s of the bot's own matches no gateway operation on %s",
            entry_id,
            target_id,
        )


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
        timestamp=datetime.now(UTC)
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
        timestamp=datetime.now(UTC)
    )
    embed.add_field(name="User ID", value=str(user.id), inline=True)

    if user.avatar_url:
        embed.set_thumbnail(user.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)

    # Check audit log for kick (member leave could be voluntary or a kick). A
    # voluntary leave has no recent matching MEMBER_KICK entry, so nothing is
    # recorded; the 10s recency window keeps a stale kick of another member from
    # being mis-attributed to this leave.
    try:
        async with get_db_session_context() as session:
            attribution = await _lookup_audit_moderator(
                bot,
                event.guild_id,
                hikari.AuditLogEventType.MEMBER_KICK,
                user.id,
                max_age_seconds=10,
                own_kinds=("kick",),
            )
            moderator_id, moderator_name, reason, entry_id = _unpack(attribution)
            if moderator_id:
                action = await mod_action_ops.create_action(
                    session,
                    guild_id=str(event.guild_id),
                    target_user_id=str(user.id),
                    target_username=user.username,
                    moderator_user_id=moderator_id,
                    moderator_username=moderator_name,
                    action_type="kick",
                    reason=reason,
                    source="audit_log",
                    audit_entry_id=entry_id,
                )
                await session.commit()
                await dispatch_mod_action(action)
    except Exception:
        log_exception(logger, f"Failed to record kick action for guild {event.guild_id}")


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
        timestamp=datetime.now(UTC)
    )
    embed.add_field(name="User ID", value=str(user.id), inline=True)

    if user.avatar_url:
        embed.set_thumbnail(user.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)

    # Record ban in moderation actions (from audit log / external source)
    try:
        async with get_db_session_context() as session:
            attribution = await _lookup_audit_moderator(
                bot,
                event.guild_id,
                hikari.AuditLogEventType.MEMBER_BAN_ADD,
                user.id,
                own_kinds=("ban",),
            )
            moderator_id, moderator_name, reason, entry_id = _unpack(attribution)
            if moderator_id:  # Only record if we found external moderator
                action = await mod_action_ops.create_action(
                    session,
                    guild_id=str(event.guild_id),
                    target_user_id=str(user.id),
                    target_username=user.username,
                    moderator_user_id=moderator_id,
                    moderator_username=moderator_name,
                    action_type="ban",
                    reason=reason,
                    source="audit_log",
                    audit_entry_id=entry_id,
                )
                await session.commit()
                await dispatch_mod_action(action)
    except Exception:
        log_exception(logger, f"Failed to record ban action for guild {event.guild_id}")


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
        timestamp=datetime.now(UTC)
    )
    embed.add_field(name="User ID", value=str(user.id), inline=True)

    if user.avatar_url:
        embed.set_thumbnail(user.avatar_url)

    await send_audit_log(bot, event.guild_id, embed)

    # Record unban in moderation actions
    try:
        async with get_db_session_context() as session:
            attribution = await _lookup_audit_moderator(
                bot,
                event.guild_id,
                hikari.AuditLogEventType.MEMBER_BAN_REMOVE,
                user.id,
            )
            moderator_id, moderator_name, reason, entry_id = _unpack(attribution)
            if moderator_id:
                action = await mod_action_ops.create_action(
                    session,
                    guild_id=str(event.guild_id),
                    target_user_id=str(user.id),
                    target_username=user.username,
                    moderator_user_id=moderator_id,
                    moderator_username=moderator_name,
                    action_type="unban",
                    reason=reason,
                    source="audit_log",
                    audit_entry_id=entry_id,
                )
                await session.commit()
                await dispatch_mod_action(action)
    except Exception:
        log_exception(logger, f"Failed to record unban action for guild {event.guild_id}")


async def log_message_edit(
    bot: hikari.GatewayBot,
    event: hikari.GuildMessageUpdateEvent
) -> None:
    """Log a message edit event with diff.

    Args:
        bot: Discord bot instance
        event: Message update event
    """
    if _was_automated(event.message.id, event.message) or (
        event.old_message is not None and is_automated(event.old_message)
    ):
        return

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

        # Create diff showing before/after; the fence counts toward the limit.
        diff = format_diff(
            event.old_message.content or "",
            event.message.content or "",
            max_length=FIELD_VALUE_LIMIT - len(DIFF_OPEN) - len(DIFF_CLOSE),
        )
        changes_value = f"{DIFF_OPEN}{diff}{DIFF_CLOSE}"
    else:
        # Message wasn't cached - show current content only
        current_content = clip(
            event.message.content or "",
            FIELD_VALUE_LIMIT - len(UNCACHED_OPEN) - len(UNCACHED_CLOSE),
        )
        changes_value = f"{UNCACHED_OPEN}{current_content}{UNCACHED_CLOSE}"

    embed = hikari.Embed(
        title="Message Edited",
        description=f"Message by {author.mention} edited in <#{event.channel_id}>",
        color=COLORS["message_edit"],
        timestamp=datetime.now(UTC)
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
    # Get message from event (if cached)
    old_message = event.old_message

    # An uncached delete is still skipped when the id was seen as automated on
    # create; one older than that memory logs as before, author unknown.
    if _was_automated(event.message_id, old_message):
        _automated_message_ids.pop(int(event.message_id), None)
        return

    if not await should_log_event(event.guild_id, "log_message_delete"):
        return

    embed = hikari.Embed(
        title="Message Deleted",
        description=f"Message deleted in <#{event.channel_id}>",
        color=COLORS["message_delete"],
        timestamp=datetime.now(UTC)
    )

    if old_message:
        author = old_message.author
        embed.add_field(name="Author", value=f"{author.mention} ({author.username})", inline=True)
        embed.add_field(name="Author ID", value=str(author.id), inline=True)

        if old_message.content:
            embed.add_field(
                name="Content", value=clip(old_message.content), inline=False
            )

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

    # Check for timeout changes (communication_disabled_until)
    old_timeout = getattr(old_member, "communication_disabled_until", None)
    new_timeout = getattr(member, "communication_disabled_until", None)
    if old_timeout != new_timeout and new_timeout is not None:
        # User was timed out — record it. Require the audit entry to actually be
        # a communication_disabled_until change (MEMBER_UPDATE also covers
        # nickname/deaf/mute) and to be recent, so an unrelated member update is
        # never mis-attributed as a timeout.
        try:
            async with get_db_session_context() as session:
                attribution = await _lookup_audit_moderator(
                    bot,
                    event.guild_id,
                    hikari.AuditLogEventType.MEMBER_UPDATE,
                    member.id,
                    change_key=hikari.AuditLogChangeKey.COMMUNICATION_DISABLED_UNTIL,
                    max_age_seconds=10,
                    own_kinds=("timeout", "untimeout"),
                )
                moderator_id, moderator_name, reason, entry_id = _unpack(attribution)

                if moderator_id:
                    # Calculate duration
                    duration_seconds = None
                    if new_timeout:
                        duration_seconds = int((new_timeout - datetime.now(UTC)).total_seconds())
                        if duration_seconds < 0:
                            duration_seconds = None

                    action = await mod_action_ops.create_action(
                        session,
                        guild_id=str(event.guild_id),
                        target_user_id=str(member.id),
                        target_username=member.username,
                        moderator_user_id=moderator_id,
                        moderator_username=moderator_name,
                        action_type="timeout",
                        reason=reason,
                        duration_seconds=duration_seconds,
                        source="audit_log",
                        audit_entry_id=entry_id,
                    )
                    await session.commit()
                    await dispatch_mod_action(action)
        except Exception:
            log_exception(logger, f"Failed to record timeout action for guild {event.guild_id}")
    elif old_timeout != new_timeout and new_timeout is None and old_timeout is not None:
        # Timeout was cleared early by a moderator — record the untimeout. Require
        # a recent communication_disabled_until change so a natural expiry (whose
        # null transition can surface on a later, unrelated member update) or any
        # other MEMBER_UPDATE entry is never mis-attributed as a moderator untimeout.
        try:
            async with get_db_session_context() as session:
                attribution = await _lookup_audit_moderator(
                    bot,
                    event.guild_id,
                    hikari.AuditLogEventType.MEMBER_UPDATE,
                    member.id,
                    change_key=hikari.AuditLogChangeKey.COMMUNICATION_DISABLED_UNTIL,
                    max_age_seconds=10,
                    own_kinds=("timeout", "untimeout"),
                )
                moderator_id, moderator_name, reason, entry_id = _unpack(attribution)

                if moderator_id:
                    action = await mod_action_ops.create_action(
                        session,
                        guild_id=str(event.guild_id),
                        target_user_id=str(member.id),
                        target_username=member.username,
                        moderator_user_id=moderator_id,
                        moderator_username=moderator_name,
                        action_type="untimeout",
                        reason=reason,
                        source="audit_log",
                        audit_entry_id=entry_id,
                    )
                    await session.commit()
                    await dispatch_mod_action(action)
        except Exception:
            log_exception(logger, f"Failed to record untimeout action for guild {event.guild_id}")

    # Check for username change
    if old_member.username != member.username:
        if await should_log_event(event.guild_id, "log_username_change"):
            embed = hikari.Embed(
                title="Username Changed",
                description=f"{member.mention} changed their username",
                color=COLORS["username_change"],
                timestamp=datetime.now(UTC)
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
                timestamp=datetime.now(UTC)
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
                timestamp=datetime.now(UTC)
            )
            embed.add_field(name="User ID", value=str(member.id), inline=True)

            if added_roles:
                roles_str = join_within([f"<@&{role_id}>" for role_id in added_roles])
                embed.add_field(name="Roles Added", value=roles_str, inline=False)

            if removed_roles:
                roles_str = join_within([f"<@&{role_id}>" for role_id in removed_roles])
                embed.add_field(name="Roles Removed", value=roles_str, inline=False)

            if member.avatar_url:
                embed.set_thumbnail(member.avatar_url)

            await send_audit_log(bot, event.guild_id, embed)
