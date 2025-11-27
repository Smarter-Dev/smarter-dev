"""Timeout command plugin for moderators."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import hikari
import lightbulb

logger = logging.getLogger(__name__)

# Create the plugin
plugin = lightbulb.Plugin("timeout")


def parse_duration(duration_str: str) -> Optional[timedelta]:
    """Parse duration string like '1h', '30m', '2d' into timedelta."""
    duration_str = duration_str.lower().strip()
    
    if not duration_str:
        return None
    
    try:
        # Extract number and unit
        if duration_str[-1] == 's':
            return timedelta(seconds=int(duration_str[:-1]))
        elif duration_str[-1] == 'm':
            return timedelta(minutes=int(duration_str[:-1]))
        elif duration_str[-1] == 'h':
            return timedelta(hours=int(duration_str[:-1]))
        elif duration_str[-1] == 'd':
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
        # Apply the timeout
        timeout_until = datetime.now().astimezone() + timeout_duration
        await ctx.bot.rest.edit_member(
            guild.id,
            target_user.id, 
            communication_disabled_until=timeout_until,
            reason=f"Timed out by {ctx.author.username}: {mod_reason}"
        )
        
        # Generate AI-processed public message
        try:
            logger.info(f"Starting AI message processing for timeout of {target_user.username}")
            from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
            ai_agent = ForumMonitorAgent()
            
            # Create a signature for timeout message processing
            import dspy
            
            class TimeoutMessageSignature(dspy.Signature):
                """You are a professional Discord moderation bot that creates polite, respectful timeout notifications.
                
                Your job is to take a moderator's raw timeout reason and convert it into a professional, community-appropriate message that:
                1. Is respectful and not inflammatory
                2. Briefly explains what happened without being accusatory  
                3. Maintains community standards
                4. Uses a calm, professional tone
                5. Focuses on the behavior, not attacking the person
                
                The message will be posted publicly in the channel where the timeout command was used.
                Keep it concise (1-2 sentences) and professional.
                
                Examples:
                - Raw: "spamming like crazy" → "<@123456789> has been timed out for excessive messaging"
                - Raw: "being a total jerk to everyone" → "<@123456789> has been timed out for inappropriate behavior toward other members"  
                - Raw: "won't stop arguing about politics" → "<@123456789> has been timed out for continuing off-topic discussions after warnings"
                """
                
                user_mention: str = dspy.InputField(description="Discord mention of the timed out user")
                raw_reason: str = dspy.InputField(description="Raw reason provided by the moderator")
                duration: str = dspy.InputField(description="Human-readable duration (e.g., '10 minutes', '1 hour')")
                polite_message: str = dspy.OutputField(description="Professional, polite timeout notification message for the channel")
            
            # Create the timeout message agent
            timeout_agent = dspy.ChainOfThought(TimeoutMessageSignature)
            
            # Format duration for display
            total_seconds = int(timeout_duration.total_seconds())
            if total_seconds < 3600:  # Less than 1 hour
                duration_display = f"{total_seconds // 60} minute{'s' if total_seconds // 60 != 1 else ''}"
            elif total_seconds < 86400:  # Less than 1 day
                hours = total_seconds // 3600
                duration_display = f"{hours} hour{'s' if hours != 1 else ''}"
            else:  # Days
                days = total_seconds // 86400
                duration_display = f"{days} day{'s' if days != 1 else ''}"
            
            # Generate the polite message
            logger.info(f"Calling AI agent with mention={target_user.mention}, reason={mod_reason}, duration={duration_display}")
            result = await dspy.asyncify(timeout_agent)(
                user_mention=target_user.mention,
                raw_reason=mod_reason,
                duration=duration_display
            )
            
            public_message = result.polite_message
            logger.info(f"AI generated message: {public_message}")
            
        except Exception as e:
            logger.error(f"Error generating AI message: {e}", exc_info=True)
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