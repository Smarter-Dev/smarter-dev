"""Moderation tools for the AI moderation agent.

Context-bound tool factory following the same closure pattern as
create_mention_tools in tools.py. Each tool operates only in the
guild/channel where moderation was triggered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import hikari
import lightbulb

from smarter_dev.bot.plugins.timeout import parse_duration
from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.models import ModerationAction

logger = logging.getLogger(__name__)

mod_action_ops = ModerationActionOperations()

# Safety: max actions per single invocation
MAX_ACTIONS_PER_INVOCATION = 3


def create_moderation_tools(
    bot: lightbulb.BotApp,
    guild_id: str,
    channel_id: str,
    trigger_message_id: str | None = None,
    enabled_tools: list[str] | None = None,
) -> list[Callable]:
    """Create context-bound moderation tools for the AI agent.

    Only tools listed in enabled_tools are returned. Each tool is
    bound to the specific guild and channel context.

    Args:
        bot: Discord bot instance
        guild_id: Guild where moderation was triggered
        channel_id: Channel where moderation was triggered
        trigger_message_id: Message that triggered the moderation review
        enabled_tools: List of tool names to enable (warn, timeout, kick, ban)

    Returns:
        List of callable async tool functions
    """
    enabled = set(enabled_tools or ["warn"])
    action_count = 0

    def _check_action_limit() -> str | None:
        nonlocal action_count
        if action_count >= MAX_ACTIONS_PER_INVOCATION:
            return f"Action limit reached ({MAX_ACTIONS_PER_INVOCATION} per invocation). No more actions allowed."
        return None

    async def _check_target_permissions(user_id: str) -> str | None:
        """Check if the target user can be moderated."""
        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            perms = lightbulb.utils.permissions_for(member)
            if perms & (hikari.Permissions.MODERATE_MEMBERS | hikari.Permissions.ADMINISTRATOR):
                return "Cannot moderate users with moderation or administrator permissions."
            if int(user_id) == bot.get_me().id:
                return "Cannot moderate the bot itself."
            return None
        except hikari.NotFoundError:
            return "User not found in this guild."
        except Exception as e:
            return f"Error checking permissions: {e}"

    async def _record_action(
        *,
        target_user_id: str,
        target_username: str,
        action_type: str,
        reason: str | None = None,
        duration_seconds: int | None = None,
        ai_context_summary: str | None = None,
    ) -> ModerationAction:
        """Record a moderation action in the database."""
        async with get_skrift_db_session_context() as session:
            action = await mod_action_ops.create_action(
                session,
                guild_id=guild_id,
                target_user_id=target_user_id,
                target_username=target_username,
                action_type=action_type,
                reason=reason,
                duration_seconds=duration_seconds,
                source="ai",
                channel_id=channel_id,
                trigger_message_id=trigger_message_id,
                ai_context_summary=ai_context_summary,
            )
            await session.commit()
            return action

    async def warn_user(user_id: str, reason: str) -> dict:
        """Issue a warning to a user. This sends a warning embed in the channel
        and DMs the user. The warning is recorded in the moderation log.

        Args:
            user_id: Discord user ID to warn
            reason: Reason for the warning

        Returns:
            dict with 'success' boolean and details
        """
        nonlocal action_count
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        perm_msg = await _check_target_permissions(user_id)
        if perm_msg:
            return {"success": False, "error": perm_msg}

        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            username = member.display_name or member.username

            # Record the action
            await _record_action(
                target_user_id=user_id,
                target_username=username,
                action_type="warn",
                reason=reason,
            )
            action_count += 1

            # Get warn count
            async with get_skrift_db_session_context() as session:
                warn_count = await mod_action_ops.count_warns_for_user(session, guild_id, user_id)

            # Send warning embed in channel
            embed = hikari.Embed(
                title="⚠️ Warning Issued",
                description=f"**{username}** has been warned.\n**Reason:** {reason}\n**Total warnings:** {warn_count}",
                color=hikari.Color(0xFFAA00),
                timestamp=datetime.now(timezone.utc),
            )
            await bot.rest.create_message(int(channel_id), embed=embed)

            # DM the user
            try:
                dm = await member.user.fetch_dm_channel()
                await bot.rest.create_message(
                    dm,
                    f"⚠️ You have received a warning in the server.\n\n**Reason:** {reason}\n**Total warnings:** {warn_count}",
                )
            except (hikari.ForbiddenError, hikari.NotFoundError):
                logger.info(f"Could not DM user {username} about warning")

            return {
                "success": True,
                "result": f"Warning issued to {username}. They now have {warn_count} total warning(s).",
            }
        except Exception as e:
            logger.error(f"[ModTool] warn_user failed: {e}")
            return {"success": False, "error": str(e)}

    async def timeout_user(user_id: str, duration: str, reason: str) -> dict:
        """Timeout (mute) a user for a specified duration. They won't be able
        to send messages or join voice channels.

        Args:
            user_id: Discord user ID to timeout
            duration: Duration string like '10m', '1h', '2d'
            reason: Reason for the timeout

        Returns:
            dict with 'success' boolean and details
        """
        nonlocal action_count
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        perm_msg = await _check_target_permissions(user_id)
        if perm_msg:
            return {"success": False, "error": perm_msg}

        td = parse_duration(duration)
        if not td:
            return {"success": False, "error": f"Invalid duration format: {duration}. Use '10m', '1h', '2d', etc."}
        if td > timedelta(days=28):
            return {"success": False, "error": "Timeout duration cannot exceed 28 days."}

        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            username = member.display_name or member.username
            timeout_until = datetime.now(timezone.utc) + td

            await bot.rest.edit_member(
                int(guild_id),
                int(user_id),
                communication_disabled_until=timeout_until,
                reason=f"AI moderation: {reason}",
            )

            duration_secs = int(td.total_seconds())
            await _record_action(
                target_user_id=user_id,
                target_username=username,
                action_type="timeout",
                reason=reason,
                duration_seconds=duration_secs,
            )
            action_count += 1

            # Notify in channel
            embed = hikari.Embed(
                title="🔇 User Timed Out",
                description=f"**{username}** has been timed out for **{duration}**.\n**Reason:** {reason}",
                color=hikari.Color(0xFF8800),
                timestamp=datetime.now(timezone.utc),
            )
            await bot.rest.create_message(int(channel_id), embed=embed)

            # DM the user
            try:
                dm = await member.user.fetch_dm_channel()
                await bot.rest.create_message(
                    dm,
                    f"🔇 You have been timed out for **{duration}**.\n\n**Reason:** {reason}\n"
                    f"Your timeout will be lifted <t:{int(timeout_until.timestamp())}:R>.",
                )
            except (hikari.ForbiddenError, hikari.NotFoundError):
                logger.info(f"Could not DM user {username} about timeout")

            return {
                "success": True,
                "result": f"{username} timed out for {duration}.",
            }
        except hikari.ForbiddenError:
            return {"success": False, "error": "Bot lacks permission to timeout this user."}
        except Exception as e:
            logger.error(f"[ModTool] timeout_user failed: {e}")
            return {"success": False, "error": str(e)}

    async def kick_user(user_id: str, reason: str) -> dict:
        """Kick a user from the server. They can rejoin with an invite link.

        Args:
            user_id: Discord user ID to kick
            reason: Reason for the kick

        Returns:
            dict with 'success' boolean and details
        """
        nonlocal action_count
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        perm_msg = await _check_target_permissions(user_id)
        if perm_msg:
            return {"success": False, "error": perm_msg}

        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            username = member.display_name or member.username

            # DM before kick (can't DM after they leave)
            try:
                dm = await member.user.fetch_dm_channel()
                await bot.rest.create_message(
                    dm,
                    f"👢 You have been kicked from the server.\n\n**Reason:** {reason}",
                )
            except (hikari.ForbiddenError, hikari.NotFoundError):
                logger.info(f"Could not DM user {username} about kick")

            await bot.rest.kick_user(int(guild_id), int(user_id), reason=f"AI moderation: {reason}")

            await _record_action(
                target_user_id=user_id,
                target_username=username,
                action_type="kick",
                reason=reason,
            )
            action_count += 1

            embed = hikari.Embed(
                title="👢 User Kicked",
                description=f"**{username}** has been kicked.\n**Reason:** {reason}",
                color=hikari.Color(0xFF4444),
                timestamp=datetime.now(timezone.utc),
            )
            await bot.rest.create_message(int(channel_id), embed=embed)

            return {"success": True, "result": f"{username} kicked from the server."}
        except hikari.ForbiddenError:
            return {"success": False, "error": "Bot lacks permission to kick this user."}
        except Exception as e:
            logger.error(f"[ModTool] kick_user failed: {e}")
            return {"success": False, "error": str(e)}

    async def ban_user(user_id: str, reason: str) -> dict:
        """Ban a user from the server permanently. They cannot rejoin unless unbanned.

        Args:
            user_id: Discord user ID to ban
            reason: Reason for the ban

        Returns:
            dict with 'success' boolean and details
        """
        nonlocal action_count
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        perm_msg = await _check_target_permissions(user_id)
        if perm_msg:
            return {"success": False, "error": perm_msg}

        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            username = member.display_name or member.username

            # DM before ban
            try:
                dm = await member.user.fetch_dm_channel()
                await bot.rest.create_message(
                    dm,
                    f"🔨 You have been banned from the server.\n\n**Reason:** {reason}",
                )
            except (hikari.ForbiddenError, hikari.NotFoundError):
                logger.info(f"Could not DM user {username} about ban")

            await bot.rest.ban_user(int(guild_id), int(user_id), reason=f"AI moderation: {reason}")

            await _record_action(
                target_user_id=user_id,
                target_username=username,
                action_type="ban",
                reason=reason,
            )
            action_count += 1

            embed = hikari.Embed(
                title="🔨 User Banned",
                description=f"**{username}** has been banned.\n**Reason:** {reason}",
                color=hikari.Color(0xCC0000),
                timestamp=datetime.now(timezone.utc),
            )
            await bot.rest.create_message(int(channel_id), embed=embed)

            return {"success": True, "result": f"{username} banned from the server."}
        except hikari.ForbiddenError:
            return {"success": False, "error": "Bot lacks permission to ban this user."}
        except Exception as e:
            logger.error(f"[ModTool] ban_user failed: {e}")
            return {"success": False, "error": str(e)}

    async def send_mod_message(message: str) -> dict:
        """Send a message in the channel as the bot. Use this for de-escalation,
        explaining a decision, or providing context. Not a moderation action.

        Args:
            message: Message content to send (max 2000 characters)

        Returns:
            dict with 'success' boolean
        """
        try:
            if len(message) > 2000:
                return {"success": False, "error": f"Message too long ({len(message)} chars). Max is 2000."}
            await bot.rest.create_message(int(channel_id), message)
            return {"success": True, "result": "Message sent."}
        except Exception as e:
            logger.error(f"[ModTool] send_mod_message failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_user_history(user_id: str) -> dict:
        """Look up a user's moderation history in this server. Returns recent
        warnings, timeouts, kicks, and bans.

        Args:
            user_id: Discord user ID to look up

        Returns:
            dict with user's moderation history
        """
        try:
            async with get_skrift_db_session_context() as session:
                actions = await mod_action_ops.get_actions_for_user(session, guild_id, user_id, limit=20)
                warn_count = await mod_action_ops.count_warns_for_user(session, guild_id, user_id)

            history = []
            for a in actions:
                entry = {
                    "type": a.action_type,
                    "reason": a.reason or "No reason given",
                    "source": a.source,
                    "date": a.created_at.strftime("%Y-%m-%d %H:%M UTC"),
                }
                if a.duration_seconds:
                    entry["duration_seconds"] = a.duration_seconds
                history.append(entry)

            return {
                "success": True,
                "total_warnings": warn_count,
                "total_actions": len(actions),
                "recent_actions": history,
            }
        except Exception as e:
            logger.error(f"[ModTool] get_user_history failed: {e}")
            return {"success": False, "error": str(e)}

    # Build tool list based on enabled tools
    tools: list[Callable] = []

    if "warn" in enabled:
        tools.append(warn_user)
    if "timeout" in enabled:
        tools.append(timeout_user)
    if "kick" in enabled:
        tools.append(kick_user)
    if "ban" in enabled:
        tools.append(ban_user)

    # Always include these utility tools
    tools.append(send_mod_message)
    tools.append(get_user_history)

    return tools
