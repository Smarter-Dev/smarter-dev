"""
Auto Moderation plugin for the Smarter Dev Discord bot.

This plugin implements auto moderation features based on rules configured in the admin interface.
"""

import re
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import hikari
import lightbulb
from lightbulb import commands, context

from bot.api_client import APIClient
from bot.api_models import AutoModRegexRule, AutoModRateLimit, ModerationCase, DiscordUser

# Create plugin
automod_plugin = lightbulb.Plugin("AutoMod")
logger = logging.getLogger("bot.plugins.automod")

# Cache for regex rules to avoid frequent API calls
# Format: {guild_id: (rules, timestamp)}
regex_rules_cache: Dict[int, tuple] = {}
# Cache timeout in seconds
CACHE_TIMEOUT = 300  # 5 minutes


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
    rule: AutoModRegexRule,
    match: re.Match
) -> None:
    """
    Apply a moderation action based on a rule.

    Args:
        client: API client
        bot: Bot instance
        guild_id: Guild ID
        user: User to moderate
        rule: Rule that matched
        match: Regex match object
    """
    # Get bot user for moderation actions
    bot_user = bot.get_me()

    # Get guild for logging
    guild = await bot.rest.fetch_guild(guild_id)

    # Prepare reason
    reason = f"Auto-moderation: Username matches pattern '{rule.pattern}'"
    if rule.description:
        reason += f" ({rule.description})"

    # Log the action
    logger.info(f"Auto-mod action: {rule.action} for user {user.username} ({user.id}) in guild {guild.name} ({guild_id})")
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
        if rule.action == "ban":
            # Ban the user
            await bot.rest.ban_user(guild_id, user.id, reason=reason)
            duration_sec = None
        elif rule.action == "kick":
            # Kick the user
            await bot.rest.kick_user(guild_id, user.id, reason=reason)
            duration_sec = None
        elif rule.action == "timeout":
            # Timeout the user (default: 1 day)
            timeout_duration = datetime.now() + timedelta(days=1)
            await bot.rest.edit_member(guild_id, user.id, communication_disabled_until=timeout_duration, reason=reason)
            duration_sec = 86400  # 1 day in seconds

        # Create moderation case
        case = ModerationCase(
            guild_id=guild_id,
            user_id=user_id,
            mod_id=bot_user_id,
            action=rule.action,
            reason=reason,
            duration_sec=duration_sec
        )

        await client.create_moderation_case(case)

    except Exception as e:
        logger.error(f"Error applying moderation action: {e}")


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
        # Apply moderation action
        await apply_moderation_action(client, event.app, event.guild_id, event.user, rule, match)
    else:
        logger.info(f"Username '{event.user.username}' did not match any rules")


def load(bot: lightbulb.BotApp) -> None:
    """Load the automod plugin."""
    bot.add_plugin(automod_plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the automod plugin."""
    bot.remove_plugin(automod_plugin)
