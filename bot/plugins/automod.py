"""
Auto Moderation plugin for the Smarter Dev Discord bot.

This plugin implements auto moderation features based on rules configured in the admin interface.
"""

import re
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple, DefaultDict
from collections import defaultdict

import hikari
import lightbulb
from lightbulb import commands, context

from bot.api_client import APIClient
from bot.api_models import AutoModRegexRule, AutoModRateLimit, ModerationCase, DiscordUser, UserWarning

# Create plugin
automod_plugin = lightbulb.Plugin("AutoMod")
logger = logging.getLogger("bot.plugins.automod")

# Cache for regex rules to avoid frequent API calls
# Format: {guild_id: (rules, timestamp)}
regex_rules_cache: Dict[int, tuple] = {}
# Cache for rate limits to avoid frequent API calls
# Format: {guild_id: (limits, timestamp)}
rate_limits_cache: Dict[int, tuple] = {}
# Cache timeout in seconds
CACHE_TIMEOUT = 300  # 5 minutes

# Message tracking for rate limits
# Format: {guild_id: {user_id: {limit_type: [(message_id, timestamp)]}}}
message_tracking: DefaultDict[int, DefaultDict[int, DefaultDict[str, List[Tuple[int, float]]]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(list))
)

# Duplicate message tracking
# Format: {guild_id: {user_id: {content_hash: [(message_id, timestamp)]}}}
duplicate_tracking: DefaultDict[int, DefaultDict[int, DefaultDict[str, List[Tuple[int, float]]]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(list))
)

# Channel tracking for rate limits
# Format: {guild_id: {user_id: {channel_id: [(message_id, timestamp)]}}}
channel_tracking: DefaultDict[int, DefaultDict[int, DefaultDict[int, List[Tuple[int, float]]]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(list))
)


async def get_regex_rules(client: APIClient, guild_id: int) -> List[AutoModRegexRule]:
    """
    Get active regex rules for a guild, with caching.

    Args:
        client: API client
        guild_id: Guild ID

    Returns:
        List of active regex rules
    """
    # Check cache first
    now = datetime.now().timestamp()
    if guild_id in regex_rules_cache:
        rules, timestamp = regex_rules_cache[guild_id]
        if now - timestamp < CACHE_TIMEOUT:
            return rules

    # Get rules from API
    try:
        # First, we need to get the database ID for this guild
        guild_response = await client._request("GET", f"/api/guilds?discord_id={guild_id}")
        guild_data = await client._get_json(guild_response)

        if not guild_data.get("guilds"):
            logger.error(f"Guild with discord_id {guild_id} not found in database")
            return []

        # Find the exact match for the Discord guild ID
        db_guild_id = None
        for guild in guild_data["guilds"]:
            if str(guild["discord_id"]) == str(guild_id):
                db_guild_id = guild["id"]
                break

        if db_guild_id is None:
            logger.error(f"Could not find exact match for guild with discord_id {guild_id}")
            return []

        logger.info(f"Fetching regex rules for guild {guild_id} (DB ID: {db_guild_id})")

        # Now get the rules using the database ID
        rules = await client.get_automod_regex_rules(guild_id=db_guild_id, is_active=True)
        logger.info(f"Received {len(rules)} regex rules for guild {guild_id}")

        # Update cache
        regex_rules_cache[guild_id] = (rules, now)
        return rules
    except Exception as e:
        logger.error(f"Error getting regex rules for guild {guild_id}: {e}")
        # Return empty list on error
        return []


async def get_rate_limits(client: APIClient, guild_id: int) -> List[AutoModRateLimit]:
    """
    Get active rate limits for a guild, with caching.

    Args:
        client: API client
        guild_id: Guild ID

    Returns:
        List of active rate limits
    """
    # Check cache first
    now = datetime.now().timestamp()
    if guild_id in rate_limits_cache:
        limits, timestamp = rate_limits_cache[guild_id]
        if now - timestamp < CACHE_TIMEOUT:
            return limits

    # Get limits from API
    try:
        # First, we need to get the database ID for this guild
        guild_response = await client._request("GET", f"/api/guilds?discord_id={guild_id}")
        guild_data = await client._get_json(guild_response)

        if not guild_data.get("guilds"):
            logger.error(f"Guild with discord_id {guild_id} not found in database")
            return []

        # Find the exact match for the Discord guild ID
        db_guild_id = None
        for guild in guild_data["guilds"]:
            if str(guild["discord_id"]) == str(guild_id):
                db_guild_id = guild["id"]
                break

        if db_guild_id is None:
            logger.error(f"Could not find exact match for guild with discord_id {guild_id}")
            return []

        logger.info(f"Fetching rate limits for guild {guild_id} (DB ID: {db_guild_id})")

        # Now get the limits using the database ID
        limits = await client.get_automod_rate_limits(guild_id=db_guild_id, is_active=True)
        logger.info(f"Received {len(limits)} rate limits for guild {guild_id}")

        # Update cache
        rate_limits_cache[guild_id] = (limits, now)
        return limits
    except Exception as e:
        logger.error(f"Error getting rate limits for guild {guild_id}: {e}")
        # Return empty list on error
        return []


async def check_username_against_rules(
    client: APIClient,
    user: hikari.User,
    guild_id: int,
    bot: lightbulb.BotApp
) -> Optional[Tuple[AutoModRegexRule, re.Match]]:
    """
    Check a username against regex rules.

    Args:
        client: API client
        user: User to check
        guild_id: Guild ID
        bot: Bot instance

    Returns:
        Tuple of (rule, match) if a rule matches, None otherwise
    """
    # Get rules
    rules = await get_regex_rules(client, guild_id)
    logger.info(f"Retrieved {len(rules)} regex rules for guild {guild_id}")
    if not rules:
        logger.info(f"No regex rules found for guild {guild_id}")
        return None

    # Get user account age
    user_created_at = user.created_at
    account_age_days = (datetime.now(user_created_at.tzinfo) - user_created_at).days
    logger.info(f"User {user.username} account age: {account_age_days} days, has avatar: {bool(user.avatar_url)}")

    # Check each rule
    for rule in rules:
        logger.info(f"Checking rule {rule.id}: pattern='{rule.pattern}', require_no_avatar={rule.require_no_avatar}, max_account_age_days={rule.max_account_age_days}")

        # Skip if rule requires no avatar but user has one
        if rule.require_no_avatar and user.avatar_url:
            logger.info(f"Skipping rule {rule.id} because user has an avatar")
            continue

        # Skip if rule has max account age and user's account is older
        if rule.max_account_age_days and account_age_days > rule.max_account_age_days:
            logger.info(f"Skipping rule {rule.id} because user's account is too old ({account_age_days} days > {rule.max_account_age_days} days)")
            continue

        # Check regex pattern
        try:
            pattern = re.compile(rule.pattern, re.IGNORECASE)
            match = pattern.search(user.username)
            if match:
                logger.info(f"Rule {rule.id} matched username '{user.username}' with pattern '{rule.pattern}'")
                return (rule, match)
            else:
                logger.info(f"Rule {rule.id} did not match username '{user.username}'")
        except re.error as e:
            logger.error(f"Invalid regex pattern in rule {rule.id}: {e}")
            continue

    return None


async def apply_moderation_action(
    client: APIClient,
    bot: lightbulb.BotApp,
    guild_id: int,
    user: hikari.User,
    action: str,
    reason: str,
    duration_sec: Optional[int] = None,
    message: Optional[hikari.Message] = None,
    channel_id: Optional[int] = None
) -> None:
    """
    Apply a moderation action.

    Args:
        client: API client
        bot: Bot instance
        guild_id: Guild ID
        user: User to moderate
        action: Action to take (ban, kick, timeout, warn)
        reason: Reason for the action
        duration_sec: Duration of timeout in seconds (if applicable)
        message: The message that triggered the action (if applicable)
        channel_id: Channel ID to send warning message to if no message is provided
    """
    # Get bot user for moderation actions
    bot_user = bot.get_me()

    # Get guild for logging
    guild = await bot.rest.fetch_guild(guild_id)

    # Log the action
    logger.info(f"Auto-mod action: {action} for user {user.username} ({user.id}) in guild {guild.name} ({guild_id})")
    logger.info(f"Reason: {reason}")

    try:
        # Get or create bot user in database
        bot_user_response = await client._request("GET", f"/api/users?discord_id={bot_user.id}")
        bot_user_data = await client._get_json(bot_user_response)

        if not bot_user_data.get("users"):
            # Create bot user
            bot_user_obj = DiscordUser(
                id=None,
                discord_id=bot_user.id,
                username=bot_user.username,
                discriminator=getattr(bot_user, "discriminator", None),
                avatar_url=bot_user.avatar_url
            )
            bot_user_response = await client._request("POST", "/api/users", data=client._dict_from_model(bot_user_obj))
            bot_user_data = await client._get_json(bot_user_response)
            bot_user_id = bot_user_data["id"]
        else:
            bot_user_id = bot_user_data["users"][0]["id"]

        # Get or create target user in database
        user_response = await client._request("GET", f"/api/users?discord_id={user.id}")
        user_data = await client._get_json(user_response)

        if not user_data.get("users"):
            # Create user
            user_obj = DiscordUser(
                id=None,
                discord_id=user.id,
                username=user.username,
                discriminator=getattr(user, "discriminator", None),
                avatar_url=user.avatar_url
            )
            user_response = await client._request("POST", "/api/users", data=client._dict_from_model(user_obj))
            user_data = await client._get_json(user_response)
            user_id = user_data["id"]
        else:
            user_id = user_data["users"][0]["id"]

        # Apply the action
        if action == "ban":
            # Ban the user
            await bot.rest.ban_user(guild_id, user.id, reason=reason)
        elif action == "kick":
            # Kick the user
            await bot.rest.kick_user(guild_id, user.id, reason=reason)
        elif action == "timeout":
            # Timeout the user
            if duration_sec:
                # Use UTC timezone for Discord API compatibility
                timeout_duration = datetime.now(user.created_at.tzinfo) + timedelta(seconds=duration_sec)
            else:
                # Default: 1 day
                timeout_duration = datetime.now(user.created_at.tzinfo) + timedelta(days=1)
                duration_sec = 86400  # 1 day in seconds

            await bot.rest.edit_member(guild_id, user.id, communication_disabled_until=timeout_duration, reason=reason)
        elif action == "warn":
            # Create a warning
            warning = UserWarning(
                guild_id=guild_id,
                user_id=user_id,
                mod_id=bot_user_id,
                reason=reason
            )
            await client._request("POST", "/api/warnings", data=client._dict_from_model(warning))

            # Reply to the user's message if provided
            if message:
                try:
                    await message.respond(f"⚠️ <@{user.id}>, **Warning**: {reason}", user_mentions=True)
                    logger.info(f"Sent warning message to user {user.username} ({user.id})")
                except Exception as e:
                    logger.error(f"Failed to send warning message: {e}")
            # Send a message to the specified channel if no message is provided but a channel_id is
            elif channel_id:
                try:
                    await bot.rest.create_message(
                        channel_id,
                        f"⚠️ <@{user.id}>, **Warning**: {reason}",
                        user_mentions=True
                    )
                    logger.info(f"Sent warning message to channel {channel_id} for user {user.username} ({user.id})")
                except Exception as e:
                    logger.error(f"Failed to send warning message to channel: {e}")

        # Create moderation case
        case = ModerationCase(
            guild_id=guild_id,
            user_id=user_id,
            mod_id=bot_user_id,
            action=action,
            reason=reason,
            duration_sec=duration_sec
        )

        await client.create_moderation_case(case)

    except Exception as e:
        logger.error(f"Error applying moderation action: {e}")


async def apply_regex_rule_action(
    client: APIClient,
    bot: lightbulb.BotApp,
    guild_id: int,
    user: hikari.User,
    rule: AutoModRegexRule,
    match: re.Match,
    channel_id: Optional[int] = None
) -> None:
    """
    Apply a moderation action based on a regex rule.

    Args:
        client: API client
        bot: Bot instance
        guild_id: Guild ID
        user: User to moderate
        rule: Rule that matched
        match: Regex match object
        channel_id: Channel ID to send warning message to (if applicable)
    """
    # Prepare reason
    reason = f"Auto-moderation: Username matches pattern '{rule.pattern}'"
    if rule.description:
        reason += f" ({rule.description})"

    await apply_moderation_action(
        client=client,
        bot=bot,
        guild_id=guild_id,
        user=user,
        action=rule.action,
        reason=reason,
        message=None,  # No message for username-based actions
        channel_id=channel_id
    )


@automod_plugin.listener(hikari.MemberCreateEvent)
async def on_member_join(event: hikari.MemberCreateEvent) -> None:
    """
    Event fired when a user joins a guild.
    Check username against regex rules.
    """
    # Get API client
    client = event.app.d.api_client

    logger.info(f"User {event.user.username} ({event.user.id}) joined guild {event.guild_id}")

    # Check username against rules
    logger.info(f"Checking username '{event.user.username}' against regex rules")
    result = await check_username_against_rules(client, event.user, event.guild_id, event.app)

    if result:
        rule, match = result
        logger.info(f"Username '{event.user.username}' matched rule: {rule.pattern}")

        # Get the guild to find a suitable channel for warnings
        try:
            guild = await event.app.rest.fetch_guild(event.guild_id)

            # Try to use the system channel (where welcome messages go)
            # If not available, we'll leave it as None and the warning will be logged but not sent
            channel_id = guild.system_channel_id

            if channel_id:
                logger.info(f"Using system channel {channel_id} for warning message")
            else:
                logger.info("No system channel found for warning message")

            # Apply moderation action
            await apply_regex_rule_action(client, event.app, event.guild_id, event.user, rule, match, channel_id)
        except Exception as e:
            logger.error(f"Error getting guild or system channel: {e}")
            # Fall back to not specifying a channel
            await apply_regex_rule_action(client, event.app, event.guild_id, event.user, rule, match)
    else:
        logger.info(f"Username '{event.user.username}' did not match any rules")


def hash_message_content(content: str) -> str:
    """
    Create a simple hash of message content for duplicate detection.

    Args:
        content: Message content

    Returns:
        A hash string of the content
    """
    # Simple hash function for duplicate detection
    # This could be improved with more sophisticated algorithms if needed
    return str(hash(content.lower().strip()))


def clean_old_messages(guild_id: int, user_id: int, current_time: float, time_threshold: float) -> None:
    """
    Clean up old messages from tracking.

    Args:
        guild_id: Guild ID
        user_id: User ID
        current_time: Current timestamp
        time_threshold: Time threshold in seconds
    """
    # Clean message tracking
    if guild_id in message_tracking and user_id in message_tracking[guild_id]:
        for limit_type, messages in list(message_tracking[guild_id][user_id].items()):
            message_tracking[guild_id][user_id][limit_type] = [
                (msg_id, timestamp) for msg_id, timestamp in messages
                if current_time - timestamp < time_threshold
            ]

    # Clean duplicate tracking
    if guild_id in duplicate_tracking and user_id in duplicate_tracking[guild_id]:
        for content_hash, messages in list(duplicate_tracking[guild_id][user_id].items()):
            duplicate_tracking[guild_id][user_id][content_hash] = [
                (msg_id, timestamp) for msg_id, timestamp in messages
                if current_time - timestamp < time_threshold
            ]

    # Clean channel tracking
    if guild_id in channel_tracking and user_id in channel_tracking[guild_id]:
        for channel_id, messages in list(channel_tracking[guild_id][user_id].items()):
            channel_tracking[guild_id][user_id][channel_id] = [
                (msg_id, timestamp) for msg_id, timestamp in messages
                if current_time - timestamp < time_threshold
            ]


async def check_rate_limits(
    client: APIClient,
    bot: lightbulb.BotApp,
    message: hikari.Message
) -> None:
    """
    Check if a message violates any rate limits.

    Args:
        client: API client
        bot: Bot instance
        message: Message to check
    """
    # Skip messages from bots
    if message.author.is_bot:
        return

    # Get guild ID and user ID
    guild_id = message.guild_id
    user_id = message.author.id

    if not guild_id:
        return  # Skip DMs

    # Get rate limits for this guild
    limits = await get_rate_limits(client, guild_id)
    if not limits:
        return  # No rate limits configured

    # Current time
    now = datetime.now().timestamp()

    # Clean up old messages first
    # Use the longest time period from all limits
    max_time_period = max([limit.time_period_seconds for limit in limits], default=3600)
    clean_old_messages(guild_id, user_id, now, max_time_period)

    # Track this message
    message_id = message.id

    # Add to message count tracking
    message_tracking[guild_id][user_id]["message_count"].append((message_id, now))

    # Add to duplicate tracking
    if message.content:
        content_hash = hash_message_content(message.content)
        duplicate_tracking[guild_id][user_id][content_hash].append((message_id, now))

    # Add to channel tracking
    channel_id = message.channel_id
    channel_tracking[guild_id][user_id][channel_id].append((message_id, now))

    # Check each limit
    for limit in limits:
        # Skip inactive limits
        if not limit.is_active:
            continue

        # Time threshold for this limit
        time_threshold = now - limit.time_period_seconds

        # Check based on limit type
        if limit.limit_type == "message_count":
            # Count messages within time period
            recent_messages = [
                msg for msg, timestamp in message_tracking[guild_id][user_id]["message_count"]
                if timestamp > time_threshold
            ]

            if len(recent_messages) >= limit.count:
                # Rate limit exceeded
                logger.info(f"Rate limit exceeded: User {message.author.username} ({user_id}) sent {len(recent_messages)} messages in {limit.time_period_seconds} seconds")

                # Apply action
                reason = f"Please slow down! You've sent too many messages in a short time ({len(recent_messages)} messages in {limit.time_period_seconds} seconds)"

                await apply_moderation_action(
                    client=client,
                    bot=bot,
                    guild_id=guild_id,
                    user=message.author,
                    action=limit.action,
                    reason=reason,
                    duration_sec=limit.action_duration_seconds,
                    message=message
                )

                # Clear tracking for this user to avoid multiple actions
                message_tracking[guild_id][user_id]["message_count"] = []

        elif limit.limit_type == "duplicate_messages" and message.content:
            # Count duplicate messages within time period
            content_hash = hash_message_content(message.content)
            duplicate_msgs = [
                msg for msg, timestamp in duplicate_tracking[guild_id][user_id][content_hash]
                if timestamp > time_threshold
            ]

            if len(duplicate_msgs) >= limit.count:
                # Duplicate message limit exceeded
                logger.info(f"Duplicate message limit exceeded: User {message.author.username} ({user_id}) sent {len(duplicate_msgs)} duplicate messages in {limit.time_period_seconds} seconds")

                # Apply action
                reason = f"Please don't send the same message repeatedly. You've sent similar messages {len(duplicate_msgs)} times in {limit.time_period_seconds} seconds"

                await apply_moderation_action(
                    client=client,
                    bot=bot,
                    guild_id=guild_id,
                    user=message.author,
                    action=limit.action,
                    reason=reason,
                    duration_sec=limit.action_duration_seconds,
                    message=message
                )

                # Clear tracking for this hash to avoid multiple actions
                duplicate_tracking[guild_id][user_id][content_hash] = []

        elif limit.limit_type == "channel_count":
            # Count unique channels within time period
            channel_msgs = {}
            for ch_id, msgs in channel_tracking[guild_id][user_id].items():
                recent_msgs = [
                    msg for msg, timestamp in msgs
                    if timestamp > time_threshold
                ]
                if recent_msgs:
                    channel_msgs[ch_id] = recent_msgs

            if len(channel_msgs) >= limit.count:
                # Channel limit exceeded
                logger.info(f"Channel limit exceeded: User {message.author.username} ({user_id}) sent messages in {len(channel_msgs)} channels in {limit.time_period_seconds} seconds")

                # Apply action
                reason = f"Please don't spam channels. You've posted in {len(channel_msgs)} different channels in {limit.time_period_seconds} seconds"

                await apply_moderation_action(
                    client=client,
                    bot=bot,
                    guild_id=guild_id,
                    user=message.author,
                    action=limit.action,
                    reason=reason,
                    duration_sec=limit.action_duration_seconds,
                    message=message
                )

                # Clear tracking for this user to avoid multiple actions
                channel_tracking[guild_id][user_id] = defaultdict(list)


@automod_plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message(event: hikari.GuildMessageCreateEvent) -> None:
    """
    Event fired when a message is sent in a guild.
    Check message against rate limits.
    """
    # Skip messages from bots
    if event.is_bot:
        return

    # Get API client
    client = event.app.d.api_client

    # Check rate limits
    await check_rate_limits(client, event.app, event.message)


def load(bot: lightbulb.BotApp) -> None:
    """Load the automod plugin."""
    bot.add_plugin(automod_plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the automod plugin."""
    bot.remove_plugin(automod_plugin)
