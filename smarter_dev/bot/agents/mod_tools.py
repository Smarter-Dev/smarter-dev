"""Moderation triage tools for the AI moderation agent.

The agent acts as a stopgap while waiting for human moderators. It can:
- Timeout users to freeze a situation
- Purge harmful messages
- Flag users for human mod review
- Send a channel message explaining the situation
- Look up user info and moderation history

Context-bound tool factory following the same closure pattern as
create_mention_tools in tools.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import hikari
import lightbulb

from smarter_dev.bot.guild_event_recorder import record_guild_event
from smarter_dev.bot.mod_action_dispatch import dispatch_mod_action
from smarter_dev.bot.plugins.timeout import parse_duration
from smarter_dev.bot.purge_core import (
    delete_selected_messages,
    select_purgeable_messages,
)
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.guild_event_log import mod_action_event
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.models import ModerationAction

logger = logging.getLogger(__name__)

mod_action_ops = ModerationActionOperations()

# Safety: at most this many destructive tool invocations (reserved slots) per
# triage. hikari may retry one invocation's request internally; there is no
# additional application-level retry.
MAX_ACTIONS_PER_INVOCATION = 3

# Anyone holding one of these, through any role, is staff and never a
# triage target.
STAFF_PERMISSIONS = (
    hikari.Permissions.ADMINISTRATOR
    | hikari.Permissions.MANAGE_GUILD
    | hikari.Permissions.MANAGE_ROLES
    | hikari.Permissions.MANAGE_CHANNELS
    | hikari.Permissions.MANAGE_MESSAGES
    | hikari.Permissions.MODERATE_MEMBERS
    | hikari.Permissions.KICK_MEMBERS
    | hikari.Permissions.BAN_MEMBERS
    | hikari.Permissions.MANAGE_NICKNAMES
    | hikari.Permissions.MANAGE_WEBHOOKS
    | hikari.Permissions.VIEW_AUDIT_LOG
)

# hikari re-sends a request it may already have delivered (timeouts,
# dropped connections, 5xx; impl/rest.py _perform_request, max_retries=3), so
# the error a destructive call finally raises says nothing about what an
# earlier attempt did: a delete retried after it went through comes back 404.
# Any error from one is therefore an unknown outcome, never a no-op.
_OUTCOME_UNKNOWN = (
    "Outcome unknown: Discord reported {}, and an earlier attempt may have "
    "applied it. Not retried."
)


# Discord epoch for snowflake timestamp decoding
DISCORD_EPOCH_MS = 1420070400000


@dataclass
class ActionTracker:
    """Tracks all actions taken during a single moderation triage run."""
    timeouts: list[dict] = field(default_factory=list)  # {user_id, username, duration, reason}
    purges: list[dict] = field(default_factory=list)  # {user_id, username, count, reason}
    deletions: list[dict] = field(default_factory=list)  # {message_id, reason}
    flags: list[str] = field(default_factory=list)  # user_ids flagged for review
    channel_message: str | None = None  # message the agent wants posted in the channel
    # Error class when the run stopped on an exception. Actions above had
    # already executed; nothing else was reviewed.
    failure: str | None = None

    @property
    def all_impacted_user_ids(self) -> set[str]:
        """Get all unique user IDs impacted by any action."""
        ids: set[str] = set()
        ids.update(self.flags)
        ids.update(t["user_id"] for t in self.timeouts)
        ids.update(p["user_id"] for p in self.purges)
        return ids

    @property
    def has_actions(self) -> bool:
        return bool(self.timeouts or self.purges or self.deletions or self.flags)


def snowflake_to_datetime(snowflake: str | int) -> datetime:
    """Decode a Discord snowflake ID to its creation timestamp."""
    timestamp_ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def build_triage_report_embed(
    tracker: ActionTracker,
    assessment: str,
    trigger_author: str,
    channel_id: str,
    trigger_message_id: str | None = None,
) -> hikari.Embed:
    """Build the summary embed posted to the mod channel after triage."""
    failed = tracker.failure is not None
    embed = hikari.Embed(
        title="Moderation Triage Failed" if failed else "Moderation Triage Report",
        description="**AI assessment (unverified):** "
        + (assessment[:300] + "..." if len(assessment) > 300 else assessment),
        color=hikari.Color(0xE74C3C if failed else 0xFFA500),
        timestamp=datetime.now(timezone.utc),
    )

    # Trigger info
    trigger_text = f"Reported by **{trigger_author}** in <#{channel_id}>"
    if trigger_message_id:
        # Discord message link format: no guild_id needed with channel link
        trigger_text += f"\n[Jump to message](https://discord.com/channels/{channel_id}/{trigger_message_id})"
    embed.add_field(name="Trigger", value=trigger_text, inline=False)

    # Flagged users
    if tracker.flags:
        flag_mentions = ", ".join(f"<@{uid}>" for uid in tracker.flags)
        embed.add_field(name="Flagged for Review", value=flag_mentions, inline=False)

    # What Discord confirmed apart from what may have happened; both come
    # from the tracker, never from the model's assessment.
    confirmed: list[str] = []
    unknown: list[str] = []
    for t in tracker.timeouts:
        line = f"Timeout **{t['username']}** for {t['duration']}: {t['reason']}"
        (unknown if t.get("possibly_applied") else confirmed).append(line)
    for p in tracker.purges:
        line = f"Purge {p['count']} message(s) from **{p['username']}**: {p['reason']}"
        (unknown if p.get("possibly_applied") else confirmed).append(line)
    for d in tracker.deletions:
        line = f"Delete msg `{d['message_id']}`: {d['reason']}"
        (unknown if d.get("possibly_applied") else confirmed).append(line)
    if confirmed or unknown:
        embed.add_field(
            name="Actions Taken (confirmed)",
            value="\n".join(confirmed)[:1024] if confirmed else "None",
            inline=False,
        )
    if unknown:
        embed.add_field(
            name="Possibly Applied (outcome unknown)",
            value="\n".join(unknown)[:1024],
            inline=False,
        )

    if failed:
        if tracker.has_actions:
            status = (
                f"Triage stopped partway ({tracker.failure}). The actions listed "
                "here were already taken; nothing else was reviewed."
            )
            if not tracker.channel_message:
                status += " No notice was posted in the channel."
        else:
            status = (
                f"Triage did not finish ({tracker.failure}). The situation was "
                "not reviewed and no action was taken."
            )
        embed.add_field(
            name="Manual Review Required",
            value=status,
            inline=False,
        )
    elif not tracker.has_actions:
        embed.add_field(
            name="No Action Taken",
            value="Situation reviewed, no immediate action required.",
            inline=False,
        )

    if failed:
        embed.set_footer(text="Automated triage failed \u2014 a moderator must review this")
    else:
        embed.set_footer(text="Automated triage \u2014 human review required")
    return embed


async def target_refusal(bot, guild_id: str, user_id: str) -> str | None:
    """Why the target may not be moderated, or None when they may.

    Fails closed: the owner, the bot and anyone holding a staff permission
    through any role (@everyone included) are refused, and so is anyone
    whose roles cannot all be resolved from the cache or, failing that,
    over REST.
    """
    try:
        if int(user_id) == bot.get_me().id:
            return "Cannot moderate the bot itself."
        member = await bot.rest.fetch_member(int(guild_id), int(user_id))
        guild = bot.cache.get_guild(int(guild_id)) or await bot.rest.fetch_guild(int(guild_id))
        if member.id == guild.owner_id:
            return "Cannot moderate the server owner."
        role_ids = {int(guild_id), *(int(r) for r in member.role_ids)}  # @everyone's id is the guild's
        roles = {rid: bot.cache.get_role(rid) for rid in role_ids}
        if not all(roles.values()):
            fetched = {int(r.id): r for r in await bot.rest.fetch_roles(int(guild_id))}
            if not role_ids <= fetched.keys():
                return "Cannot resolve this user's roles; not moderating them."
            roles = {rid: fetched[rid] for rid in role_ids}
        perms = hikari.Permissions.NONE
        for role in roles.values():
            perms |= role.permissions
        if perms & STAFF_PERMISSIONS:
            return "Cannot moderate users with staff permissions."
        return None
    except hikari.NotFoundError:
        return "User not found in this guild."
    except Exception as e:
        return f"Error checking permissions: {e}"


def create_moderation_tools(
    bot: lightbulb.BotApp,
    guild_id: str,
    channel_id: str,
    trigger_message_id: str | None = None,
    enabled_tools: list[str] | None = None,
) -> tuple[list[Callable], ActionTracker]:
    """Create context-bound triage tools for the AI moderation agent.

    Returns both the tools list and an ActionTracker that accumulates
    everything the agent does during the run.

    Args:
        bot: Discord bot instance
        guild_id: Guild where moderation was triggered
        channel_id: Channel where moderation was triggered (incident channel)
        trigger_message_id: Message that triggered the moderation review
        enabled_tools: Action tools to enable (timeout, purge, delete); None or []
            enables none. The utility tools are always included.

    Returns:
        Tuple of (tools list, ActionTracker)
    """
    # Only the action tools a guild names are offered; None or [] offers none.
    enabled = set(enabled_tools or ())
    tracker = ActionTracker()
    action_count = 0

    def _check_action_limit() -> str | None:
        if action_count >= MAX_ACTIONS_PER_INVOCATION:
            return f"Action limit reached ({MAX_ACTIONS_PER_INVOCATION} per invocation). No more actions allowed."
        return None

    # A slot is taken just before each destructive call and never given
    # back: success, partial success and every unknown outcome, cancellation
    # included, count. Only refusals before the call (limit, arguments,
    # permissions, lookups) take no slot.
    def _reserve_action() -> str | None:
        nonlocal action_count
        limit_msg = _check_action_limit()
        if limit_msg is None:
            action_count += 1
        return limit_msg

    async def _check_target_permissions(user_id: str) -> str | None:
        return await target_refusal(bot, guild_id, user_id)

    async def _record_action(
        *,
        target_user_id: str,
        target_username: str,
        action_type: str,
        reason: str | None = None,
        duration_seconds: int | None = None,
        ai_context_summary: str | None = None,
    ) -> ModerationAction | None:
        """Record an action that already happened on Discord, best-effort.

        Callers count and track the action before this, so a failed write
        never hides it from the action limit or the report.
        """
        try:
            return await _write_action(
                target_user_id=target_user_id,
                target_username=target_username,
                action_type=action_type,
                reason=reason,
                duration_seconds=duration_seconds,
                ai_context_summary=ai_context_summary,
            )
        except Exception:
            logger.exception(f"[ModTool] recording {action_type} for {target_user_id} failed")
            return None

    async def _write_action(
        *,
        target_user_id: str,
        target_username: str,
        action_type: str,
        reason: str | None,
        duration_seconds: int | None,
        ai_context_summary: str | None,
    ) -> ModerationAction:
        async with get_db_session_context() as session:
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
        # Fire the mod_action trigger so a mod-log handler formats the AI action
        # (best-effort; never breaks the triage tool). The bot goes along so the
        # same call writes the action into the guild's short-term event log.
        await dispatch_mod_action(action, bot=bot)
        return action

    # ── Action tools ─────────────────────────────────────────────────

    async def timeout_user(user_id: str, duration: str, reason: str) -> dict:
        """Timeout (mute) a user for a specified duration. They won't be able
        to send messages or join voice channels. Use this to freeze a situation
        while waiting for human moderators.

        Prefer short durations (10m-30m) unless the situation is severe.

        Args:
            user_id: Discord user ID to timeout
            duration: Duration string like '10m', '30m', '1h'
            reason: Reason for the timeout

        Returns:
            dict with 'success' boolean and details
        """
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        td = parse_duration(duration)
        if not td or td <= timedelta(0):
            return {"success": False, "error": f"Invalid duration format: {duration}. Use '10m', '1h', '2d', etc."}
        if td > timedelta(hours=1):
            return {"success": False, "error": "Triage timeouts cannot exceed 1 hour. Human mods can extend if needed."}

        perm_msg = await _check_target_permissions(user_id)
        if perm_msg:
            return {"success": False, "error": perm_msg}

        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
        except Exception as e:
            return {"success": False, "error": f"Could not fetch user: {e}"}
        username = member.display_name or member.username
        timeout_until = datetime.now(timezone.utc) + td
        entry = {"user_id": user_id, "username": username, "duration": duration, "reason": reason}

        limit_msg = _reserve_action()
        if limit_msg:
            return {"success": False, "error": limit_msg}
        try:
            await bot.rest.edit_member(
                int(guild_id),
                int(user_id),
                communication_disabled_until=timeout_until,
                reason=f"AI triage: {reason}",
            )
        except asyncio.CancelledError:
            tracker.timeouts.append({**entry, "possibly_applied": True})
            raise
        except Exception as e:
            logger.error(f"[ModTool] timeout_user failed: {e!r}")
            tracker.timeouts.append({**entry, "possibly_applied": True})
            return {"success": False, "error": _OUTCOME_UNKNOWN.format(type(e).__name__)}
        tracker.timeouts.append(entry)

        await _record_action(
            target_user_id=user_id,
            target_username=username,
            action_type="timeout",
            reason=reason,
            duration_seconds=int(td.total_seconds()),
        )

        # DM the user
        try:
            dm = await member.user.fetch_dm_channel()
            await bot.rest.create_message(
                dm,
                f"You have been temporarily timed out for **{duration}**.\n\n**Reason:** {reason}\n"
                f"Your timeout will be lifted <t:{int(timeout_until.timestamp())}:R>.\n"
                f"A moderator will review the situation shortly.",
            )
        except Exception:
            logger.info(f"Could not DM user {username} about timeout")

        return {"success": True, "result": f"{username} timed out for {duration}."}

    async def purge_messages(user_id: str, count: int, reason: str) -> dict:
        """Delete recent messages from a user in this channel. Use this to
        remove harmful content (spam, slurs, threats, NSFW) to limit exposure.

        Args:
            user_id: Discord user ID whose messages to delete
            count: Number of messages to delete (max 50)
            reason: Reason for purging the messages

        Returns:
            dict with 'success' boolean and count of messages deleted
        """
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        count = min(count, 50)  # Cap at 50

        perm_msg = await _check_target_permissions(user_id)
        if perm_msg:
            return {"success": False, "error": perm_msg}

        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            username = member.display_name or member.username

            # Shared paging/bulk-delete core (14-day rule + single-vs-bulk delete)
            # so /purge and this tool never diverge on which messages they delete.
            selection = await select_purgeable_messages(
                bot.rest.fetch_messages(int(channel_id)).limit(count * 3),
                count=count,
                user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[ModTool] purge_messages failed: {e}")
            return {"success": False, "error": str(e)}
        if not selection.message_ids:
            return {"success": True, "result": f"No recent messages found from user {username} to delete."}
        entry = {"user_id": user_id, "username": username, "reason": reason}

        limit_msg = _reserve_action()
        if limit_msg:
            return {"success": False, "error": limit_msg}
        selected = len(selection.message_ids)
        try:
            await delete_selected_messages(bot.rest, int(channel_id), selection.message_ids)
            deleted_count = selected
        except asyncio.CancelledError:
            tracker.purges.append({**entry, "count": selected, "possibly_applied": True})
            raise
        except Exception as e:
            logger.error(f"[ModTool] purge_messages failed: {e!r}")
            # hikari lists the messages of every chunk Discord acknowledged;
            # the rest of the selection has an unknown outcome.
            confirmed = len(e.deleted_messages) if isinstance(e, hikari.BulkDeleteError) else 0
            if confirmed:
                tracker.purges.append({**entry, "count": confirmed})
                await _record_action(
                    target_user_id=user_id,
                    target_username=username,
                    action_type="purge",
                    reason=reason,
                    ai_context_summary=f"Purged {confirmed} message(s)",
                )
            tracker.purges.append({**entry, "count": selected - confirmed, "possibly_applied": True})
            failure = e.__cause__ if isinstance(e, hikari.BulkDeleteError) and e.__cause__ else e
            return {"success": False, "error": _OUTCOME_UNKNOWN.format(type(failure).__name__)}
        tracker.purges.append({**entry, "count": deleted_count})

        await _record_action(
            target_user_id=user_id,
            target_username=username,
            action_type="purge",
            reason=reason,
            ai_context_summary=f"Purged {deleted_count} message(s)",
        )

        return {"success": True, "result": f"Deleted {deleted_count} message(s) from {username}."}

    async def delete_message(message_id: str, reason: str) -> dict:
        """Delete a single message by its ID. Use this for surgical removal of
        a specific harmful message when you don't need to bulk-purge.

        Message IDs are shown in the conversation context as [msg:ID].

        Args:
            message_id: Discord message ID to delete
            reason: Reason for deleting the message

        Returns:
            dict with 'success' boolean
        """
        limit_msg = _check_action_limit()
        if limit_msg:
            return {"success": False, "error": limit_msg}

        try:
            message = await bot.rest.fetch_message(int(channel_id), int(message_id))
        except hikari.NotFoundError:
            return {"success": False, "error": f"Message {message_id} not found."}
        except Exception as e:
            logger.error(f"[ModTool] delete_message failed: {e}")
            return {"success": False, "error": str(e)}
        perm_msg = await _check_target_permissions(str(message.author.id))
        if perm_msg:
            return {"success": False, "error": perm_msg}
        entry = {"message_id": message_id, "reason": reason}

        limit_msg = _reserve_action()
        if limit_msg:
            return {"success": False, "error": limit_msg}
        try:
            await bot.rest.delete_message(int(channel_id), int(message_id))
        except asyncio.CancelledError:
            tracker.deletions.append({**entry, "possibly_applied": True})
            raise
        except Exception as e:
            logger.error(f"[ModTool] delete_message failed: {e!r}")
            tracker.deletions.append({**entry, "possibly_applied": True})
            return {"success": False, "error": _OUTCOME_UNKNOWN.format(type(e).__name__)}
        tracker.deletions.append(entry)

        # TODO(§3.8): a single delete writes no ModerationAction row today, so
        # the bot's short-term memory is captured here. Once feature-parity
        # §3.8 gives it a row it will flow through dispatch_mod_action — drop
        # this call then, or the delete lands in the log twice.
        try:
            await record_guild_event(
                bot,
                mod_action_event(
                    {
                        "action_type": "delete",
                        "reason": reason,
                        "source": "ai",
                        "channel_id": channel_id,
                    },
                    guild_id=guild_id,
                ),
            )
        except Exception:
            logger.exception(f"[ModTool] recording delete of {message_id} failed")

        return {"success": True, "result": f"Message {message_id} deleted."}

    # ── Utility tools (always available) ─────────────────────────────

    async def flag_users(user_ids: str) -> dict:
        """Flag one or more users for human moderator review. This does NOT
        notify the users — it only includes them in the mod report.

        Use this to mark users whose behavior needs human attention, even if
        you don't timeout or purge them.

        Args:
            user_ids: Comma-separated Discord user IDs to flag (e.g. "123,456,789")

        Returns:
            dict with 'success' boolean
        """
        ids = [uid.strip() for uid in user_ids.split(",") if uid.strip()]
        if not ids:
            return {"success": False, "error": "No user IDs provided."}
        tracker.flags.extend(ids)
        return {"success": True, "result": f"Flagged {len(ids)} user(s) for moderator review."}

    async def send_mod_message(message: str) -> dict:
        """Compose a message to be posted in the channel addressing the situation.
        This message will be sent after all actions are complete.

        IMPORTANT: Do NOT mention any users by name or @ in this message.
        The system will automatically append user pings.

        Use a firm but professional moderator tone. If actions were taken,
        explain why. If no actions were taken, explain what behavior must stop.

        Args:
            message: Message content (max 1800 characters, pings are appended)

        Returns:
            dict with 'success' boolean
        """
        if len(message) > 1800:
            return {"success": False, "error": f"Message too long ({len(message)} chars). Max is 1800."}
        tracker.channel_message = message
        return {"success": True, "result": "Channel message composed. It will be sent after triage completes."}

    async def get_user_info(user_id: str) -> dict:
        """Get information about a user including their account age and
        when they joined the server. Useful for identifying new/throwaway accounts.

        Args:
            user_id: Discord user ID to look up

        Returns:
            dict with user info including account_created, joined_server, roles
        """
        try:
            member = await bot.rest.fetch_member(int(guild_id), int(user_id))
            username = member.display_name or member.username

            account_created = snowflake_to_datetime(user_id)
            account_age_days = (datetime.now(timezone.utc) - account_created).days

            joined_at = member.joined_at
            membership_days = (datetime.now(timezone.utc) - joined_at).days if joined_at else None

            role_names = []
            for role_id in member.role_ids:
                try:
                    role = member.get_guild().get_role(role_id)
                    if role and role.name != "@everyone":
                        role_names.append(role.name)
                except Exception:
                    pass

            return {
                "success": True,
                "username": username,
                "user_id": user_id,
                "is_bot": member.is_bot,
                "account_created": account_created.strftime("%Y-%m-%d %H:%M UTC"),
                "account_age_days": account_age_days,
                "joined_server": joined_at.strftime("%Y-%m-%d %H:%M UTC") if joined_at else "Unknown",
                "membership_days": membership_days,
                "roles": role_names,
            }
        except hikari.NotFoundError:
            return {"success": False, "error": "User not found in this guild."}
        except Exception as e:
            logger.error(f"[ModTool] get_user_info failed: {e}")
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
            async with get_db_session_context() as session:
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

    # ── Build tool list ──────────────────────────────────────────────
    tools: list[Callable] = []

    if "timeout" in enabled:
        tools.append(timeout_user)
    if "purge" in enabled:
        tools.append(purge_messages)
    if "delete" in enabled:
        tools.append(delete_message)

    # Always include utility tools
    tools.append(flag_users)
    tools.append(send_mod_message)
    tools.append(get_user_info)
    tools.append(get_user_history)

    return tools, tracker
