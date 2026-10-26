"""Timeout command plugin for moderators."""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta

import hikari
import lightbulb
from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.utility_model import build_utility_agent
from smarter_dev.bot.agents.utility_model import render_inputs
from smarter_dev.bot.agents.utility_model import utility_metered
from smarter_dev.bot.moderation_port import bot_gateway
from smarter_dev.bot.moderation_port import manual_trigger
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import types as gw

logger = logging.getLogger(__name__)

# Create the plugin
plugin = lightbulb.Plugin("timeout")

TIMEOUT_MESSAGE_INSTRUCTIONS = """You are a Discord moderation bot that writes the public notice posted when a moderator times a member out.

Your job is to turn the moderator's raw reason into a short notice that keeps the moderator's point in their own words:
1. Keep the moderator's own keywords and key phrases. Carry their wording through rather than swapping it for a generic summary like "inappropriate behavior" or "not engaging respectfully".
2. Rephrase second-person jabs aimed at the member into the third person ("you never listen" becomes "not listening"), keeping the rest of the phrase as close to the moderator's wording as you can. A general saying such as "you don't know what you don't know" can stay as it is.
3. Drop insults, profanity and personal jabs; keep the words that describe the behavior.
4. Stay calm and not inflammatory, and describe the behavior rather than attacking the person.

The message will be posted publicly in the channel where the timeout command was used.
Keep it concise (1-2 sentences) and mention the duration.

Examples:
- Raw: "you have no idea what you don't know so learn to listen to others who do" → "<@123456789> has been timed out for 1 day for not listening to others who know. You don't know what you don't know."
- Raw: "spamming like crazy" → "<@123456789> has been timed out for 10 minutes for spamming."
- Raw: "being a total jerk to everyone, stop insulting people's code" → "<@123456789> has been timed out for 1 hour for insulting people's code."
- Raw: "won't stop arguing about politics after 3 warnings" → "<@123456789> has been timed out for 2 hours for arguing about politics after 3 warnings."

## Inputs
- user_mention: Discord mention of the timed out user
- raw_reason: Raw reason provided by the moderator
- duration: Human-readable duration (e.g., '10 minutes', '1 hour')
"""


class TimeoutMessage(BaseModel):
    polite_message: str = Field(
        description="Timeout notice for the channel, keeping the moderator's own wording"
    )


_timeout_message_agent: Agent[None, TimeoutMessage] | None = None


def get_timeout_message_agent() -> Agent[None, TimeoutMessage]:
    """Return the singleton timeout-announcement agent, building it on first use."""
    global _timeout_message_agent
    if _timeout_message_agent is None:
        _timeout_message_agent = build_utility_agent(
            TimeoutMessage, TIMEOUT_MESSAGE_INSTRUCTIONS
        )
    return _timeout_message_agent


def format_duration(duration: timedelta) -> str:
    """Render a timeout length as minutes, hours or days for the announcement."""
    total_seconds = int(duration.total_seconds())
    if total_seconds < 3600:  # Less than 1 hour
        minutes = total_seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if total_seconds < 86400:  # Less than 1 day
        hours = total_seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = total_seconds // 86400
    return f"{days} day{'s' if days != 1 else ''}"


async def generate_timeout_message(
    user_mention: str,
    raw_reason: str,
    duration_display: str,
    *,
    moderator_id: int | str | None = None,
    guild_id: int | str | None = None,
    channel_id: int | str | None = None,
) -> str:
    """Rewrite the moderator's reason as a polite public announcement.

    The run's spend is the moderator's, in the channel the command ran in."""
    agent = get_timeout_message_agent()
    async with utility_metered(
        "timeout_message",
        discord_user_id=moderator_id,
        guild_id=guild_id,
        channel_id=channel_id,
    ) as meter:
        result = await agent.run(
            render_inputs(
                user_mention=user_mention,
                raw_reason=raw_reason,
                duration=duration_display,
            ),
            model=meter.wrap(agent.model),
        )
    return result.output.polite_message.strip()


def parse_duration(duration_str: str) -> timedelta | None:
    """Parse duration string like '1h', '30m', '2d' into timedelta."""
    duration_str = duration_str.lower().strip()

    if not duration_str:
        return None

    try:
        # Extract number and unit
        if duration_str[-1] == "s":
            return timedelta(seconds=int(duration_str[:-1]))
        elif duration_str[-1] == "m":
            return timedelta(minutes=int(duration_str[:-1]))
        elif duration_str[-1] == "h":
            return timedelta(hours=int(duration_str[:-1]))
        elif duration_str[-1] == "d":
            return timedelta(days=int(duration_str[:-1]))
        else:
            # Try parsing as minutes if no unit
            return timedelta(minutes=int(duration_str))
    except (ValueError, IndexError):
        return None


@plugin.command
@lightbulb.option(
    "reason",
    "Reason for the timeout",
    type=hikari.OptionType.STRING,
    required=True
)
@lightbulb.option(
    "duration",
    "Duration (e.g., '10m', '1h', '2d')",
    type=hikari.OptionType.STRING,
    required=True
)
@lightbulb.option(
    "user",
    "The user to timeout",
    type=hikari.OptionType.USER,
    required=True
)
@lightbulb.command(
    "timeout",
    "Timeout a user with a specified duration and reason"
)
@lightbulb.implements(lightbulb.SlashCommand)
async def timeout_user(ctx: lightbulb.Context) -> None:
    """Timeout a user with AI-processed messaging."""

    # Get command parameters
    target_user = ctx.options.user
    duration_str = ctx.options.duration
    mod_reason = ctx.options.reason

    # Check if user has timeout members permission
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond("❌ This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    member_perms = lightbulb.utils.permissions_for(ctx.member)
    if not (member_perms & hikari.Permissions.MODERATE_MEMBERS):
        await ctx.respond("❌ You don't have permission to timeout members.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Parse duration
    timeout_duration = parse_duration(duration_str)
    if not timeout_duration:
        await ctx.respond("❌ Invalid duration format. Use formats like '10m', '1h', '2d'.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Check if duration is within Discord limits (max 28 days)
    if timeout_duration > timedelta(days=28):
        await ctx.respond("❌ Timeout duration cannot exceed 28 days.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Check if we can timeout this user (bot permissions, role hierarchy, etc.)
    try:
        guild = ctx.get_guild()
        if not guild:
            await ctx.respond("❌ Could not access guild information.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Get the target member
        try:
            target_member = guild.get_member(target_user.id)
            if not target_member:
                target_member = await ctx.bot.rest.fetch_member(guild.id, target_user.id)
        except hikari.NotFoundError:
            await ctx.respond("❌ User is not a member of this server.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Check if target user is bot owner or has higher role
        bot_member = guild.get_member(ctx.bot.get_me().id)
        if not bot_member:
            bot_member = await ctx.bot.rest.fetch_member(guild.id, ctx.bot.get_me().id)

        bot_perms = lightbulb.utils.permissions_for(bot_member)
        if not (bot_perms & hikari.Permissions.MODERATE_MEMBERS):
            await ctx.respond("❌ I don't have permission to timeout members.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Don't timeout other moderators/admins
        target_perms = lightbulb.utils.permissions_for(target_member)
        if (target_perms & (hikari.Permissions.MODERATE_MEMBERS | hikari.Permissions.ADMINISTRATOR)):
            await ctx.respond("❌ Cannot timeout users with moderation permissions.", flags=hikari.MessageFlag.EPHEMERAL)
            return

    except Exception as e:
        logger.error(f"Error checking permissions for timeout: {e}")
        await ctx.respond("❌ Error checking permissions. Please try again.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Defer response as we'll be doing AI processing (ephemeral so only moderator sees "thinking")
    await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)

    try:
        # Apply the timeout through the gateway: serialised with the automation
        # on this member, recorded once, and from then on a timeout the
        # automation leaves alone.
        result = await bot_gateway(ctx.bot).act(
            manual_trigger(
                guild_id=guild.id,
                channel_id=ctx.channel_id,
                moderator=ctx.author,
                reason=mod_reason,
            ),
            [
                Proposal(
                    kind=gw.TIMEOUT,
                    target_user_id=str(target_user.id),
                    target_username=target_user.username,
                    reason=f"Timed out by {ctx.author.username}: {mod_reason}",
                    duration_seconds=int(timeout_duration.total_seconds()),
                )
            ],
        )
        outcome = result.outcomes[0]
        if not outcome.applied:
            if "permission" in outcome.detail:
                await ctx.edit_last_response("❌ I don't have permission to timeout this user.")
            elif outcome.state == gw.UNKNOWN:
                await ctx.edit_last_response(
                    "❌ Discord did not confirm the timeout; it is being checked. "
                    "Look at the member before trying again."
                )
            else:
                await ctx.edit_last_response(f"❌ Failed to timeout user: {outcome.detail}")
            return
        timeout_until = outcome.expires_at or datetime.now().astimezone() + timeout_duration

        duration_display = format_duration(timeout_duration)

        # Generate AI-processed public message
        try:
            logger.info(f"Starting AI message processing for timeout of {target_user.username}")
            public_message = await generate_timeout_message(
                target_user.mention,
                mod_reason,
                duration_display,
                moderator_id=ctx.author.id,
                guild_id=guild.id,
                channel_id=ctx.channel_id,
            )
            logger.info(f"AI generated message: {public_message}")
        except Exception as e:
            logger.error(f"Error generating AI message: {e}", exc_info=True)
            public_message = ""
        if not public_message:
            # Fallback to simple message
            public_message = f"{target_user.mention} has been timed out for {duration_display}."

        # Update the ephemeral response for moderator and post public message
        logger.info(f"Posting public message: {public_message}")
        await ctx.edit_last_response("✅ Timeout applied successfully.")
        await ctx.bot.rest.create_message(ctx.channel_id, public_message)

        # Send DM to the timed out user
        try:
            dm_channel = await target_user.fetch_dm_channel()
            dm_message = (
                f"You have been timed out in **{guild.name}** for **{duration_display}**.\n\n"
                f"**Moderator's reason:** {mod_reason}\n\n"
                f"Your timeout will be lifted on <t:{int(timeout_until.timestamp())}:F>.\n"
                f"If you have questions about this action, please contact the server moderators."
            )
            await ctx.bot.rest.create_message(dm_channel, dm_message)
        except (hikari.ForbiddenError, hikari.NotFoundError):
            # User has DMs disabled or blocked the bot
            logger.info(f"Could not DM user {target_user.username} about timeout")
            # Optionally add a note to the public message that DM failed

        # Log the timeout action
        logger.info(f"User {target_user.username} ({target_user.id}) timed out by {ctx.author.username} "
                   f"({ctx.author.id}) for {duration_display}. Reason: {mod_reason}")

    except hikari.ForbiddenError:
        await ctx.edit_last_response("❌ I don't have permission to timeout this user.")
    except hikari.BadRequestError as e:
        await ctx.edit_last_response(f"❌ Failed to timeout user: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during timeout: {e}")
        await ctx.edit_last_response("❌ An unexpected error occurred. Please try again.")


def load(bot: lightbulb.BotApp) -> None:
    """Load the timeout plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the timeout plugin."""
    bot.remove_plugin(plugin)
