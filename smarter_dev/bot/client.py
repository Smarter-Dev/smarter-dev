"""Discord bot client setup and configuration."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import hikari
import lightbulb

from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings
from smarter_dev.bot.services.api_client import APIClient

logger = logging.getLogger(__name__)


async def store_streak_celebration(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_username: str,
    user_message: str,
    bot_response: str,
    tokens_used: int,
    streak_days: int,
    streak_multiplier: int,
    bytes_earned: int,
    response_time_ms: int | None = None
) -> bool:
    """Store a streak celebration interaction in the database for auditing and analytics.
    
    Args:
        guild_id: Discord guild ID
        channel_id: Discord channel ID
        user_id: Discord user ID
        user_username: Username at time of interaction
        user_message: User's message that triggered the streak
        bot_response: Bot's celebration response
        tokens_used: AI tokens consumed
        streak_days: Number of days in the streak
        streak_multiplier: Streak bonus multiplier
        bytes_earned: Total bytes earned
        response_time_ms: Response generation time
        
    Returns:
        bool: True if stored successfully, False otherwise
    """
    try:
        # Generate session ID
        session_id = str(uuid.uuid4())
        
        # Prepare conversation data
        conversation_data = {
            "session_id": session_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "user_username": user_username,
            "interaction_type": "streak_celebration",
            "context_messages": [],  # No context needed for streak celebrations
            "user_question": user_message[:2000],  # User's triggering message
            "bot_response": bot_response[:4000],  # Celebration message
            "tokens_used": tokens_used,
            "response_time_ms": response_time_ms,
            "retention_policy": "standard",
            "is_sensitive": False,
            # Streak-specific metadata for analytics
            "command_metadata": {
                "command_type": "streak_celebration",
                "streak_days": streak_days,
                "streak_multiplier": streak_multiplier,
                "bytes_earned": bytes_earned,
                "message_length": len(user_message)
            }
        }
        
        # Store conversation via API
        settings = get_settings()
        async with APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=10.0
        ) as client:
            response = await client.post("/admin/conversations", json_data=conversation_data)
            if response.status_code in (200, 201):
                logger.debug(f"✅ Stored streak celebration for user {user_id} (session: {session_id})")
                return True
            else:
                logger.warning(f"❌ Failed to store streak celebration: HTTP {response.status_code}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Error storing streak celebration: {e}")
        return False


# Cache to track users who have already claimed their daily reward today
# Format: {f"{guild_id}:{user_id}": "YYYY-MM-DD"}
daily_claim_cache: dict[str, str] = {}


@dataclass
class ForumPostData:
    """Data structure for forum post information."""
    title: str
    content: str
    author_display_name: str
    tags: list[str]
    attachments: list[str]
    channel_id: str
    thread_id: str
    guild_id: str


def get_utc_date_string() -> str:
    """Get current UTC date as YYYY-MM-DD string."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def has_claimed_today(guild_id: str, user_id: str) -> bool:
    """Check if user has already claimed their daily reward today."""
    cache_key = f"{guild_id}:{user_id}"
    today = get_utc_date_string()
    return daily_claim_cache.get(cache_key) == today


def mark_claimed_today(guild_id: str, user_id: str) -> None:
    """Mark user as having claimed their daily reward today."""
    cache_key = f"{guild_id}:{user_id}"
    today = get_utc_date_string()
    daily_claim_cache[cache_key] = today
    logger.debug(f"Marked {user_id} as claimed for {today} in guild {guild_id}")


def cleanup_old_cache_entries() -> None:
    """Remove cache entries from previous days to prevent memory leaks."""
    today = get_utc_date_string()
    old_keys = [key for key, date_str in daily_claim_cache.items() if date_str != today]
    for key in old_keys:
        del daily_claim_cache[key]
    if old_keys:
        logger.debug(f"Cleaned up {len(old_keys)} old cache entries")


async def sync_squad_roles(
    bot: lightbulb.BotApp,
    event: hikari.GuildMessageCreateEvent
) -> None:
    """Sync Discord squad roles with database squad membership.

    Ensures that the user's Discord roles match their squad membership in the database:
    - If user has squad in DB but missing Discord role: Add the role
    - If user has squad role in Discord but not in DB: Remove the role
    - If roles don't match: Remove old role and add correct one

    Args:
        bot: Bot application instance
        event: Message create event with guild, author, and member info
    """
    try:
        guild_id = str(event.guild_id)
        user_id = str(event.author.id)

        # Get services
        squads_service = getattr(bot, "d", {}).get("squads_service")
        if not squads_service:
            logger.debug("Squads service not available for role sync")
            return

        # Get guild and member
        guild = event.get_guild()
        if not guild:
            logger.debug(f"Could not get guild {guild_id} for role sync")
            return

        member = guild.get_member(user_id)
        if not member:
            logger.debug(f"Could not get member {user_id} for role sync")
            return

        # Get user's current squad from database
        try:
            user_squad_response = await squads_service.get_user_squad(guild_id, user_id, use_cache=False)
            user_squad = user_squad_response.squad if user_squad_response else None
        except Exception as e:
            logger.debug(f"Failed to get user squad for role sync: {e}")
            return

        # Get all squads for the guild to identify squad role IDs
        try:
            all_squads = await squads_service.list_squads(guild_id)
        except Exception as e:
            logger.debug(f"Failed to list squads for role sync: {e}")
            return

        squad_role_ids = {int(squad.role_id) for squad in all_squads}

        # Find user's current squad roles in Discord
        user_squad_roles = [role_id for role_id in member.role_ids if int(role_id) in squad_role_ids]

        # Determine what actions to take
        expected_role_id = int(user_squad.role_id) if user_squad else None
        current_role_ids = set(user_squad_roles)

        roles_to_remove = []
        roles_to_add = []

        if expected_role_id:
            # User should have a squad role
            if expected_role_id not in current_role_ids:
                # Missing the correct role, add it
                roles_to_add.append(expected_role_id)

            # Remove any other squad roles they might have
            for role_id in current_role_ids:
                if role_id != expected_role_id:
                    roles_to_remove.append(role_id)
        else:
            # User should not have any squad roles
            if current_role_ids:
                # Remove all squad roles (database is source of truth)
                roles_to_remove = list(current_role_ids)

        # Apply role changes
        for role_id in roles_to_remove:
            try:
                role = guild.get_role(role_id)
                if role:
                    await member.remove_role(role)
                    logger.debug(f"Removed squad role {role.name} from user {user_id}")
            except Exception as e:
                logger.warning(f"Failed to remove squad role {role_id} from user {user_id}: {e}")

        for role_id in roles_to_add:
            try:
                role = guild.get_role(role_id)
                if role:
                    await member.add_role(role)
                    squad_name = user_squad.name if user_squad else "Unknown"
                    logger.info(f"Synced squad role {role.name} to user {user_id} in squad '{squad_name}'")
            except Exception as e:
                logger.warning(f"Failed to add squad role {role_id} to user {user_id}: {e}")

        if not roles_to_remove and not roles_to_add:
            logger.debug(f"Squad roles already in sync for user {user_id}")

    except Exception as e:
        logger.error(f"Unexpected error syncing squad roles for user {event.author.id}: {e}")


# Fun and techy status messages that rotate every 5 minutes
STATUS_MESSAGES = [
    "🚀 Compiling bytes...",
    "⚡ Optimizing algorithms",
    "🔧 Debugging the matrix",
    "💾 Caching quantum data",
    "🌐 Syncing with the cloud",
    "🤖 Training neural networks",
    "📡 Scanning for packets",
    "🔍 Indexing the interwebs",
    "⚙️ Refactoring reality",
    "🎯 Targeting efficiency",
    "🔐 Encrypting secrets",
    "📊 Analyzing patterns",
    "🌟 Generating awesomeness",
    "🔄 Looping infinitely",
    "💡 Processing genius ideas",
    "🚨 Monitoring systems",
    "📈 Scaling to infinity",
    "🔋 Charging batteries",
    "🎨 Rendering pixels",
    "🌊 Surfing data streams",
    "🔥 Burning rubber",
    "⭐ Collecting stardust",
    "🎲 Rolling random numbers",
    "🧠 Computing intelligence",
    "🎵 Harmonizing frequencies",
]


async def start_status_rotation(bot: lightbulb.BotApp) -> None:
    """Start the periodic status message rotation.
    
    Args:
        bot: Bot application instance
    """
    async def rotate_status():
        """Rotate the bot's status message every 5 minutes."""
        while True:
            try:
                # Pick a random status message
                status_message = random.choice(STATUS_MESSAGES)

                # Update bot's activity
                await bot.update_presence(
                    activity=hikari.Activity(
                        name=status_message,
                        type=hikari.ActivityType.CUSTOM
                    )
                )

                logger.debug(f"Updated bot status to: {status_message}")

                # Wait 5 minutes before next rotation
                await asyncio.sleep(300)  # 300 seconds = 5 minutes

            except Exception as e:
                logger.error(f"Error rotating status: {e}")
                # Wait a bit before retrying
                await asyncio.sleep(60)

    # Start the rotation task
    asyncio.create_task(rotate_status())
    logger.info("Started status rotation (5-minute intervals)")


async def start_cache_cleanup() -> None:
    """Start the periodic cache cleanup for daily claim tracking."""
    async def cleanup_cache():
        """Clean up old cache entries every hour."""
        while True:
            try:
                # Clean up old entries
                cleanup_old_cache_entries()

                # Wait 1 hour before next cleanup
                await asyncio.sleep(3600)  # 3600 seconds = 1 hour

            except Exception as e:
                logger.error(f"Error cleaning up cache: {e}")
                # Wait a bit before retrying
                await asyncio.sleep(300)  # 5 minutes

    # Start the cleanup task
    asyncio.create_task(cleanup_cache())
    logger.info("Started daily claim cache cleanup (hourly intervals)")


async def initialize_single_guild_configuration(guild_id: str) -> None:
    """Initialize bytes configuration for a single guild using the API.
    
    The API automatically creates a default configuration if none exists
    when requesting the guild configuration.
    
    Args:
        guild_id: Discord guild ID to initialize
    """
    try:
        from smarter_dev.bot.services.api_client import APIClient
        from smarter_dev.shared.config import get_settings

        settings = get_settings()

        # Create API client
        api_client = APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key
        )

        # Get guild configuration - this automatically creates one with defaults if it doesn't exist
        await api_client.get(f"/guilds/{guild_id}/bytes/config")
        logger.info(f"✅ Ensured bytes configuration exists for guild {guild_id}")

    except Exception as e:
        logger.error(f"Failed to initialize guild configuration for {guild_id}: {e}")


async def initialize_guild_configurations(bot: lightbulb.BotApp) -> None:
    """Initialize bytes configurations for all guilds the bot is in.
    
    Args:
        bot: Bot application instance
    """
    try:
        # Get all guilds the bot is currently in
        guilds = bot.cache.get_guilds_view()

        logger.info(f"Initializing configurations for {len(guilds)} guilds...")

        for guild_id in guilds:
            await initialize_single_guild_configuration(str(guild_id))

        logger.info("✅ Guild configuration initialization complete")

    except Exception as e:
        logger.error(f"Failed to initialize guild configurations: {e}")


def create_bot(settings: Settings | None = None) -> lightbulb.BotApp:
    """Create and configure the Discord bot with Lightbulb v2 syntax.
    
    Returns:
        BotApp instance for v2 compatibility
    """
    if settings is None:
        settings = get_settings()

    # Configure bot intents
    intents = (
        hikari.Intents.GUILDS  # Covers thread events including forum thread creation
        | hikari.Intents.GUILD_MEMBERS  # For member tracking
        | hikari.Intents.GUILD_MESSAGES  # For activity tracking
        | hikari.Intents.MESSAGE_CONTENT  # For message content
        | hikari.Intents.GUILD_MESSAGE_REACTIONS  # For message reactions
    )

    # Create bot instance using Lightbulb BotApp (v2 syntax)
    bot = lightbulb.BotApp(
        token=settings.discord_bot_token,
        intents=intents,
        logs={
            "version": 1,
            "incremental": True,
            "loggers": {
                "hikari": {"level": "INFO"},
                "hikari.ratelimits": {"level": "DEBUG"},
                "lightbulb": {"level": "INFO"},
                "smarter_dev": {"level": "DEBUG"},  # Enable DEBUG logging for the application
            },
        },
        banner=None,  # Disable banner for cleaner logs
    )

    return bot


async def setup_bot_services(bot: lightbulb.BotApp) -> None:
    """Set up bot services and dependencies."""
    logger.info("Setting up bot services...")

    try:
        # Get settings
        settings = get_settings()

        # Create API client
        from smarter_dev.bot.services.api_client import APIClient
        api_base_url = settings.api_base_url
        api_key = settings.bot_api_key
        logger.info(f"Connecting to API at: {api_base_url}")
        logger.info(f"Using API key: {api_key[:12]}...{api_key[-10:] if len(api_key) > 20 else api_key}")
        api_client = APIClient(
            base_url=api_base_url,  # Web API base URL from settings
            api_key=api_key,  # Use secure API key for auth
            default_timeout=30.0
        )

        # Bot doesn't use caching - pass None for cache manager
        cache_manager = None

        # Create services
        from smarter_dev.bot.services.bytes_service import BytesService
        from smarter_dev.bot.services.challenge_service import ChallengeService
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        from smarter_dev.bot.services.scheduled_message_service import (
            ScheduledMessageService,
        )
        from smarter_dev.bot.services.repeating_message_service import (
            RepeatingMessageService,
        )
        from smarter_dev.bot.services.squads_service import SquadsService
        from smarter_dev.bot.services.channel_state import initialize_channel_state_manager

        bytes_service = BytesService(api_client, cache_manager)
        squads_service = SquadsService(api_client, cache_manager)
        forum_agent_service = ForumAgentService(api_client, cache_manager)
        challenge_service = ChallengeService(api_client, cache_manager, bot)
        scheduled_message_service = ScheduledMessageService(api_client, cache_manager, bot)
        repeating_message_service = RepeatingMessageService(api_client, cache_manager, bot)

        # Initialize conversation participation services
        channel_state_manager = initialize_channel_state_manager()

        # Initialize services
        logger.info("Initializing bytes service...")
        await bytes_service.initialize()
        logger.info("✓ Bytes service initialized")

        logger.info("Initializing squads service...")
        await squads_service.initialize()
        logger.info("✓ Squads service initialized")

        logger.info("Initializing forum agent service...")
        await forum_agent_service.initialize()
        logger.info("✓ Forum agent service initialized")

        logger.info("Initializing challenge service...")
        await challenge_service.initialize()
        logger.info("✓ Challenge service initialized")

        logger.info("Initializing scheduled message service...")
        await scheduled_message_service.initialize()
        logger.info("✓ Scheduled message service initialized")

        logger.info("Initializing repeating message service...")
        await repeating_message_service.initialize()
        logger.info("✓ Repeating message service initialized")

        # Verify service health
        logger.info("Verifying service health...")
        try:
            bytes_health = await bytes_service.health_check()
            squads_health = await squads_service.health_check()
            forum_agent_health = await forum_agent_service.health_check()
            challenge_health = await challenge_service.health_check()
            scheduled_message_health = await scheduled_message_service.health_check()
            repeating_message_health = await repeating_message_service.health_check()

            logger.info(f"Bytes service health: {'healthy' if bytes_health.is_healthy else 'unhealthy'}")
            logger.info(f"Squads service health: {'healthy' if squads_health.is_healthy else 'unhealthy'}")
            logger.info(f"Forum agent service health: {'healthy' if forum_agent_health.is_healthy else 'unhealthy'}")
            logger.info(f"Challenge service health: {'healthy' if challenge_health.is_healthy else 'unhealthy'}")
            logger.info(f"Scheduled message service health: {'healthy' if scheduled_message_health.is_healthy else 'unhealthy'}")
            logger.info(f"Repeating message service health: {'healthy' if repeating_message_health.is_healthy else 'unhealthy'}")

            if not bytes_health.is_healthy:
                logger.warning(f"Bytes service not healthy: {bytes_health.details}")
            if not squads_health.is_healthy:
                logger.warning(f"Squads service not healthy: {squads_health.details}")
            if not forum_agent_health.is_healthy:
                logger.warning(f"Forum agent service not healthy: {forum_agent_health.details}")
            if not challenge_health.is_healthy:
                logger.warning(f"Challenge service not healthy: {challenge_health.details}")
            if not scheduled_message_health.is_healthy:
                logger.warning(f"Scheduled message service not healthy: {scheduled_message_health.details}")
            if not repeating_message_health.is_healthy:
                logger.warning(f"Repeating message service not healthy: {repeating_message_health.details}")

        except Exception as e:
            logger.error(f"Failed to check service health: {e}")

        # Store services in bot data
        if not hasattr(bot, "d"):
            bot.d = {}

        bot.d["api_client"] = api_client
        bot.d["cache_manager"] = cache_manager
        bot.d["bytes_service"] = bytes_service
        bot.d["squads_service"] = squads_service
        bot.d["forum_agent_service"] = forum_agent_service
        bot.d["challenge_service"] = challenge_service
        bot.d["scheduled_message_service"] = scheduled_message_service
        bot.d["repeating_message_service"] = repeating_message_service
        bot.d["channel_state_manager"] = channel_state_manager

        # Store services in d for plugin access (primary)
        bot.d["_services"] = {
            "bytes_service": bytes_service,
            "squads_service": squads_service,
            "forum_agent_service": forum_agent_service,
            "challenge_service": challenge_service,
            "scheduled_message_service": scheduled_message_service,
            "repeating_message_service": repeating_message_service,
            "channel_state_manager": channel_state_manager
        }

        logger.info("✓ Bot services setup complete")
        logger.info(f"Services available: {list(bot.d.keys())}")
        logger.info(f"Plugin services: {list(bot.d['_services'].keys())}")

    except Exception as e:
        logger.error(f"Failed to setup bot services: {e}")
        # Set empty services to prevent crashes
        if not hasattr(bot, "d"):
            bot.d = {}
        bot.d["_services"] = {}


def is_forum_channel(channel) -> bool:
    """Check if a channel is a forum channel.
    
    Args:
        channel: Discord channel object
        
    Returns:
        True if channel is a forum channel
    """
    return hasattr(channel, "type") and channel.type == hikari.ChannelType.GUILD_FORUM


async def extract_forum_post_data(bot: lightbulb.BotApp, thread, initial_message=None) -> ForumPostData:
    """Extract forum post data from Discord thread and message objects.
    
    Args:
        thread: Discord thread object
        initial_message: Initial message in the thread (forum post content)
        
    Returns:
        ForumPostData object with extracted information
    """
    # Extract basic information
    title = getattr(thread, "name", "")
    thread_id = str(getattr(thread, "id", ""))
    channel_id = str(getattr(thread, "parent_id", ""))

    # Extract message information if available
    if initial_message:
        content = getattr(initial_message, "content", "")
        author = getattr(initial_message, "author", None)
        author_name = getattr(author, "display_name", getattr(author, "username", "Unknown")) if author else "Unknown"

        # Extract attachments
        attachments = []
        if hasattr(initial_message, "attachments"):
            attachments = [getattr(att, "filename", "unknown") for att in initial_message.attachments]

        # Debug extracted data
        logger.debug(f"FORUM EXTRACT DEBUG: Content: '{content[:100]}...' ({len(content)} chars)")
        logger.debug(f"FORUM EXTRACT DEBUG: Author: '{author_name}'")
        logger.debug(f"FORUM EXTRACT DEBUG: Attachments: {len(attachments)}")
    else:
        content = ""
        author_name = "Unknown"
        attachments = []
        logger.warning("FORUM EXTRACT DEBUG: No initial message provided - content and author will be empty/unknown")

    # Extract tags if available
    tags = []
    if hasattr(thread, "applied_tag_ids") and thread.applied_tag_ids:
        tag_ids = getattr(thread, "applied_tag_ids", [])
        logger.warning(f"FORUM TAG DEBUG - Found {len(tag_ids)} applied tag IDs: {tag_ids}")
        # Resolve tag IDs to tag names
        tags = await resolve_forum_tag_names(bot, channel_id, tag_ids)
    else:
        logger.warning("FORUM TAG DEBUG - No applied_tag_ids found on thread")

    return ForumPostData(
        title=title,
        content=content,
        author_display_name=author_name,
        tags=tags,
        attachments=attachments,
        channel_id=channel_id,
        thread_id=thread_id,
        guild_id=""  # Will be set by caller
    )


async def post_agent_responses(bot: lightbulb.BotApp, thread_id: int, responses: list[dict]) -> bool:
    """Post AI agent responses to a Discord thread.
    
    Args:
        bot: Discord bot instance
        thread_id: Thread ID to post responses to
        responses: List of agent response dictionaries
        
    Returns:
        bool: True if any response was posted, False otherwise
    """
    if not responses:
        return False

    response_posted = False
    try:
        for response_data in responses:
            # Only post if agent decided to respond
            if not response_data.get("should_respond", False):
                continue

            response_content = response_data.get("response_content", "").strip()
            if not response_content:
                continue

            # Use just the raw response content (no agent identification)
            formatted_response = response_content

            # Post the response to the thread
            await bot.rest.create_message(
                thread_id,
                content=formatted_response
            )

            logger.info(f"Posted response to thread {thread_id}")
            response_posted = True

    except Exception as e:
        logger.error(f"Error posting agent responses to thread {thread_id}: {e}")
    
    return response_posted


async def post_user_notifications(bot: lightbulb.BotApp, thread_id: int, topic_user_map: dict, response_posted: bool) -> None:
    """Post user notification mentions to a Discord thread, organized by topic.
    
    Args:
        bot: Discord bot instance
        thread_id: Thread ID to post notifications to
        topic_user_map: Dictionary mapping topics to sets of user mention strings
        response_posted: Whether an agent response was already posted
    """
    if not topic_user_map:
        return

    try:
        # Format notification message organized by topic
        # Example: -# JavaScript @user1 @user2\n-# Frontend @user3 @user4
        notification_lines = []
        all_user_ids = set()
        
        for topic, user_mentions in topic_user_map.items():
            mentions_text = " ".join(sorted(user_mentions))  # Sort for consistency
            notification_lines.append(f"-# {topic} {mentions_text}")
            
            # Collect all unique user IDs for the user_mentions parameter
            for mention in user_mentions:
                user_id = int(mention.strip('<@>'))
                all_user_ids.add(user_id)
        
        notification_message = "\n".join(notification_lines)

        # Post the notification message with user mentions enabled
        await bot.rest.create_message(
            thread_id,
            content=notification_message,
            user_mentions=list(all_user_ids)
        )

        total_users = len(all_user_ids)
        total_topics = len(topic_user_map)
        logger.info(f"Posted user notifications to thread {thread_id}: {total_users} users notified across {total_topics} topics")

    except Exception as e:
        logger.error(f"Error posting user notifications to thread {thread_id}: {e}")


async def resolve_forum_tag_names(bot: lightbulb.BotApp, channel_id: str, tag_ids: list) -> list:
    """Resolve forum tag IDs to tag names by fetching forum channel info.
    
    Args:
        bot: Discord bot instance
        channel_id: Forum channel ID
        tag_ids: List of tag IDs to resolve
        
    Returns:
        List of tag names
    """
    try:
        if not tag_ids:
            return []

        # Fetch the forum channel to get available tags
        forum_channel = await bot.rest.fetch_channel(channel_id)

        if not hasattr(forum_channel, "available_tags"):
            logger.warning(f"Forum channel {channel_id} has no available_tags attribute")
            return [str(tag_id) for tag_id in tag_ids]  # Fall back to IDs

        # Create mapping of tag ID to tag name
        tag_map = {}
        if forum_channel.available_tags:
            for tag in forum_channel.available_tags:
                tag_map[str(tag.id)] = tag.name

        # Resolve tag IDs to names
        tag_names = []
        for tag_id in tag_ids:
            tag_id_str = str(tag_id)
            if tag_id_str in tag_map:
                tag_names.append(tag_map[tag_id_str])
            else:
                logger.warning(f"Tag ID {tag_id} not found in forum channel available tags")
                tag_names.append(f"Unknown-{tag_id}")

        logger.warning(f"FORUM TAG DEBUG - Resolved {len(tag_ids)} tag IDs to names: {tag_names}")
        return tag_names

    except Exception as e:
        logger.error(f"Error resolving forum tag names: {e}")
        return [str(tag_id) for tag_id in tag_ids]  # Fall back to IDs


async def handle_forum_thread_create(bot: lightbulb.BotApp, event) -> None:
    """Handle forum thread creation events for AI agent processing.
    
    Args:
        bot: Discord bot instance
        event: Thread creation event
    """
    logger.info(f"DEBUG: handle_forum_thread_create called for thread {event.thread.id}")

    # Check if this is a forum thread
    if not getattr(event, "is_forum_thread", True):
        logger.info("DEBUG: Not a forum thread, skipping")
        return

    # Check if we have a guild context
    if not hasattr(event, "guild_id") or not event.guild_id:
        return

    # Get forum agent service
    forum_agent_service = getattr(bot, "d", {}).get("forum_agent_service")
    if not forum_agent_service:
        forum_agent_service = getattr(bot, "d", {}).get("_services", {}).get("forum_agent_service")

    if not forum_agent_service:
        logger.debug("No forum agent service available for thread creation")
        return

    try:
        # Fetch the initial message (forum post content)
        initial_message = None
        try:
            # Get the first message in the thread (the forum post)
            # Messages are returned in reverse chronological order (newest first)
            messages = await bot.rest.fetch_messages(event.thread.id)
            logger.debug(f"FORUM DEBUG: Fetched {len(messages) if messages else 0} messages for thread {event.thread.id}")

            if messages:
                # Get the last message (oldest, which should be the initial forum post)
                initial_message = messages[-1]
                logger.debug(f"FORUM DEBUG: Initial message found - Author: {getattr(initial_message.author, 'display_name', 'Unknown')}, Content length: {len(getattr(initial_message, 'content', ''))}")
            else:
                logger.warning(f"FORUM DEBUG: No messages found in thread {event.thread.id}")
        except Exception as e:
            logger.error(f"Could not fetch initial message for thread {event.thread.id}: {e}")

        # Log thread details for debugging
        logger.warning(f"FORUM TAG DEBUG - Thread details: id={event.thread.id}, name={event.thread.name}")
        logger.warning(f"FORUM TAG DEBUG - Thread attributes: {[attr for attr in dir(event.thread) if not attr.startswith('_')]}")
        logger.warning(f"FORUM TAG DEBUG - Applied tags attribute exists: {hasattr(event.thread, 'applied_tags')}")
        if hasattr(event.thread, "applied_tags"):
            logger.warning(f"FORUM TAG DEBUG - Applied tags raw: {event.thread.applied_tags}")
        else:
            logger.warning("FORUM TAG DEBUG - No applied_tags attribute found")

        # Extract post data from the thread and initial message
        post_data = await extract_forum_post_data(bot, event.thread, initial_message)
        post_data.guild_id = str(event.guild_id)

        # Process the post through all applicable agents with user tagging support
        responses, topic_user_map = await forum_agent_service.process_forum_post_with_tagging(
            str(event.guild_id), post_data
        )

        # Post responses that should be posted
        response_posted = False
        if responses:
            response_posted = await post_agent_responses(bot, event.thread.id, responses)

        # Send user notifications if there are any
        if topic_user_map:
            await post_user_notifications(bot, event.thread.id, topic_user_map, response_posted)

    except Exception as e:
        logger.error(f"Error handling forum thread creation: {e}")


async def handle_forum_message_create(bot: lightbulb.BotApp, event) -> None:
    """Handle message creation in forum threads (follow-up messages).
    
    Args:
        bot: Discord bot instance
        event: Message creation event
    """
    # For now, we only process initial forum posts (thread creation)
    # Follow-up messages are not processed by agents
    # This function exists for potential future expansion
    pass


async def cleanup_bot_services(bot: lightbulb.BotApp) -> None:
    """Clean up bot services and connections."""
    logger.info("Cleaning up bot services...")

    try:
        # Clean up services
        if hasattr(bot, "d") and "challenge_service" in bot.d:
            await bot.d["challenge_service"].cleanup()

        if hasattr(bot, "d") and "scheduled_message_service" in bot.d:
            await bot.d["scheduled_message_service"].cleanup()

        if hasattr(bot, "d") and "repeating_message_service" in bot.d:
            await bot.d["repeating_message_service"].cleanup()

        # Clean up cache manager (if used)
        if hasattr(bot, "d") and "cache_manager" in bot.d and bot.d["cache_manager"]:
            await bot.d["cache_manager"].cleanup()

        # Clean up API client
        if hasattr(bot, "d") and "api_client" in bot.d:
            await bot.d["api_client"].close()

        logger.info("Bot services cleanup complete")

    except Exception as e:
        logger.error(f"Error cleaning up bot services: {e}")


def load_plugins(bot: lightbulb.BotApp) -> None:
    """Load bot plugins using Lightbulb v2 syntax."""
    try:
        # Check if services are available before loading plugins
        if hasattr(bot, "d") and "_services" in bot.d:
            logger.info(f"Services available for plugins: {list(bot.d['_services'].keys())}")
        else:
            logger.warning("No services found in bot.d - plugins may not work correctly")

        # Load bytes commands plugin
        logger.info("Loading bytes plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.bytes")
        logger.info("✓ Loaded bytes plugin")

        # Load squads commands plugin
        logger.info("Loading squads plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.squads")
        logger.info("✓ Loaded squads plugin")

        # Load help agent plugin
        logger.info("Loading help plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.help")
        logger.info("✓ Loaded help plugin")

        # Load mention handler plugin
        logger.info("Loading mention plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.mention")
        logger.info("✓ Loaded mention plugin")

        # Load LLM features plugin
        logger.info("Loading LLM plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.llm")
        logger.info("✓ Loaded LLM plugin")

        # Load events plugin for component interactions
        logger.info("Loading events plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.events")
        logger.info("✓ Loaded events plugin")

        # Load challenges plugin
        logger.info("Loading challenges plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.challenges")
        logger.info("✓ Loaded challenges plugin")

        # Load forum notifications plugin
        logger.info("Loading forum notifications plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.forum_notifications")
        logger.info("✓ Loaded forum notifications plugin")

        # Load timeout plugin
        logger.info("Loading timeout plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.timeout")
        logger.info("✓ Loaded timeout plugin")

        logger.info("✓ All plugins loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load plugins: {e}")
        import traceback
        logger.error(f"Plugin loading traceback: {traceback.format_exc()}")
        # Don't raise to prevent bot from crashing - just log the error
        logger.warning("Bot will run without plugins")


async def run_bot() -> None:
    """Run the Discord bot with Lightbulb v2 syntax."""
    settings = get_settings()

    if not settings.discord_bot_token:
        logger.error("Discord bot token not provided")
        return

    if not settings.discord_application_id:
        logger.error("Discord application ID not provided")
        return

    if not settings.bot_api_key:
        logger.error("Bot API key not provided")
        return

    # Create bot
    bot = create_bot(settings)
    # Set up event handlers
    @bot.listen()
    async def on_starting(event: hikari.StartingEvent) -> None:
        """Handle bot starting event."""
        logger.info("Bot is starting...")

    @bot.listen()
    async def on_started(event: hikari.StartedEvent) -> None:
        """Handle bot started event."""
        bot_user = event.app.get_me()
        if bot_user:
            logger.info(f"Bot started as {bot_user.username}#{bot_user.discriminator}")
        else:
            logger.info("Bot started")

        # Initialize guild configurations for all guilds the bot is in
        await initialize_guild_configurations(bot)

        # Start status rotation
        await start_status_rotation(bot)

        # Start cache cleanup
        await start_cache_cleanup()

        # Start conversation watcher for natural participation
        logger.info("Bot is now fully ready and will stay online")

    @bot.listen()
    async def on_stopping(event: hikari.StoppingEvent) -> None:
        """Handle bot stopping event."""
        logger.info("Bot is stopping...")
        await cleanup_bot_services(bot)

    @bot.listen()
    async def on_ready(event: hikari.ShardReadyEvent) -> None:
        """Handle shard ready event."""
        logger.info(f"Shard {event.shard.id} is ready")
        logger.info("Bot is now fully ready and will stay online")

    @bot.listen()
    async def on_guild_join(event: hikari.GuildJoinEvent) -> None:
        """Handle bot joining a new guild."""
        logger.info(f"Bot joined guild: {event.guild.name} (ID: {event.guild_id})")

        # Initialize configuration for the new guild
        await initialize_single_guild_configuration(str(event.guild_id))

        logger.info(f"✅ Initialized configuration for guild {event.guild.name}")

    @bot.listen()
    async def on_message_create(event: hikari.GuildMessageCreateEvent) -> None:
        """Handle daily bytes reward on first message each day."""
        # Skip bot messages
        if event.is_bot:
            return

        # Skip if no guild
        if not event.guild_id:
            return

        # Skip if user doesn't exist
        if not event.author:
            return

        # Get services
        bytes_service = getattr(bot, "d", {}).get("bytes_service")
        if not bytes_service:
            bytes_service = getattr(bot, "d", {}).get("_services", {}).get("bytes_service")

        if not bytes_service:
            logger.warning("No bytes service available for daily message reward")
            return

        # Check cache first to avoid unnecessary API calls
        guild_id_str = str(event.guild_id)
        user_id_str = str(event.author.id)

        if has_claimed_today(guild_id_str, user_id_str):
            # User already claimed today, skip API call
            logger.debug(f"User {event.author} already claimed daily reward today (cached)")
            return

        try:
            # Try to claim daily reward (this will only succeed on first message of the day)
            logger.debug(f"Attempting daily reward for {event.author} (ID: {event.author.id}) in guild {event.guild_id}")

            result = await bytes_service.claim_daily(
                guild_id_str,
                user_id_str,
                event.author.display_name or event.author.username
            )

            if result.success:
                # Mark as claimed in cache to prevent future API calls today
                mark_claimed_today(guild_id_str, user_id_str)

                # Handle squad auto-assignment if user was assigned to a squad
                if result.squad_assignment:
                    try:
                        squad = result.squad_assignment
                        role_id = int(squad["role_id"])
                        squad_name = squad["name"]

                        # Get the guild and member
                        guild = event.get_guild()
                        if guild:
                            member = guild.get_member(event.author.id)
                            role = guild.get_role(role_id)

                            if member and role:
                                # Add the squad role to the user
                                await member.add_role(role)
                                logger.info(f"✅ Auto-assigned user {event.author} to squad '{squad_name}' with role {role.name}")
                            else:
                                logger.warning(f"Could not assign squad role: member={member is not None}, role={role is not None}")
                        else:
                            logger.warning("Could not get guild for squad role assignment")
                    except Exception as e:
                        logger.error(f"Failed to assign squad role during daily claim: {e}")

                # Add reaction to the message that earned bytes
                try:
                    await event.message.add_reaction("daily_bytes_received:1403748840477163642")
                    logger.info(f"✅ Added reaction and awarded daily bytes reward ({result.earned}) to {event.author}")
                except Exception as e:
                    logger.error(f"Failed to add reaction to daily reward message: {e}")

                # Generate celebratory message for streak bonuses
                if result.streak_bonus and result.streak_bonus > 1:
                    try:
                        # Record start time for response time tracking
                        start_time = datetime.now()
                        
                        # Get or create streak celebration agent
                        streak_agent = getattr(bot, "d", {}).get("streak_celebration_agent")
                        if not streak_agent:
                            from smarter_dev.bot.agents.streak_agent import StreakCelebrationAgent
                            streak_agent = StreakCelebrationAgent()
                            if not hasattr(bot, "d"):
                                bot.d = {}
                            bot.d["streak_celebration_agent"] = streak_agent

                        # Generate celebration message
                        celebration_message, tokens_used = await streak_agent.generate_celebration_message(
                            bytes_earned=result.earned or 0,
                            streak_multiplier=result.streak_bonus or 1,
                            streak_days=result.streak or 0,
                            user_id=event.author.id,
                            user_message=event.message.content or ""
                        )

                        # Calculate response time
                        response_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                        # Send the celebration message with the @ mention built in
                        if celebration_message:
                            await bot.rest.create_message(
                                channel=event.channel_id,
                                content=celebration_message,
                                user_mentions=[event.author.id]
                            )
                            logger.info(f"✅ Posted streak celebration message for {event.author} (streak: {result.streak}, multiplier: {result.streak_bonus}x)")

                            # Store the streak celebration interaction for analytics
                            await store_streak_celebration(
                                guild_id=str(event.guild_id),
                                channel_id=str(event.channel_id),
                                user_id=str(event.author.id),
                                user_username=event.author.username,
                                user_message=event.message.content or "",
                                bot_response=celebration_message,
                                tokens_used=tokens_used,
                                streak_days=result.streak or 0,
                                streak_multiplier=result.streak_bonus or 1,
                                bytes_earned=result.earned or 0,
                                response_time_ms=response_time_ms
                            )

                    except Exception as e:
                        logger.error(f"Failed to generate or post streak celebration message: {e}")
            else:
                logger.debug(f"Daily reward not successful for {event.author}")

            # Sync squad roles to ensure Discord roles match database state
            await sync_squad_roles(bot, event)

        except Exception as e:
            # Handle expected scenarios gracefully
            error_str = str(e).lower()
            if ("already been claimed" in error_str or
                "already claimed" in error_str or
                "409" in error_str or
                "conflict" in error_str):
                # Mark as claimed in cache to prevent future API calls today
                mark_claimed_today(guild_id_str, user_id_str)
                logger.debug(f"Daily reward already claimed today for {event.author} (from API): {e}")
            else:
                logger.error(f"Unexpected error in daily reward for {event.author}: {e}", exc_info=True)

    @bot.listen()
    async def on_interaction_create(event: hikari.InteractionCreateEvent) -> None:
        """Handle component interactions for views."""
        if not isinstance(event.interaction, hikari.ComponentInteraction):
            return

        # Store active views in bot data for interaction routing
        if not hasattr(bot, "d"):
            bot.d = {}
        if "active_views" not in bot.d:
            bot.d["active_views"] = {}

        # Handle squad-related interactions
        custom_id = event.interaction.custom_id
        user_id = str(event.interaction.user.id)

        if custom_id in ["squad_select", "squad_confirm", "squad_cancel"]:
            logger.info(f"Received {custom_id} interaction from user {user_id}")

            # Check if there's an active view for this user
            view_key = f"{user_id}_{custom_id.split('_')[0]}"  # user_id_squad
            active_view = bot.d["active_views"].get(view_key)

            if active_view:
                try:
                    await active_view.handle_interaction(event)
                except Exception as e:
                    logger.error(f"Error handling interaction {custom_id}: {e}")
                    # Send error response if the view couldn't handle it
                    try:
                        from smarter_dev.bot.utils.embeds import create_error_embed
                        embed = create_error_embed("An error occurred while processing your selection.")
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_UPDATE,
                            embed=embed,
                            components=[]
                        )
                    except:
                        pass  # Interaction might already be responded to
            else:
                logger.warning(f"No active view found for {custom_id} interaction from user {user_id}")
                # Send timeout message
                try:
                    from smarter_dev.bot.utils.embeds import create_error_embed
                    embed = create_error_embed("This interaction has expired. Please try the command again.")
                    await event.interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_UPDATE,
                        embed=embed,
                        components=[]
                    )
                except:
                    pass  # Interaction might already be responded to

    @bot.listen()
    async def on_guild_thread_create(event: hikari.GuildThreadCreateEvent) -> None:
        """Handle forum thread creation for AI agent processing."""
        logger.info(f"FORUM DEBUG: Thread creation detected: {event.thread.id} in channel {event.thread.parent_id}, type: {event.thread.type}")

        # Only process forum threads
        if not event.thread.type == hikari.ChannelType.GUILD_PUBLIC_THREAD:
            logger.info(f"FORUM DEBUG: Skipping non-public thread: {event.thread.type}")
            return

        # Check if parent is a forum channel
        try:
            parent_channel = bot.cache.get_guild_channel(event.thread.parent_id)
            if not parent_channel or not is_forum_channel(parent_channel):
                return
        except:
            return

        # Create a mock event object for the handler
        class MockForumEvent:
            def __init__(self, thread, guild_id):
                self.thread = thread
                self.guild_id = guild_id
                self.is_forum_thread = True

        mock_event = MockForumEvent(event.thread, event.guild_id)
        await handle_forum_thread_create(bot, mock_event)

    @bot.listen()
    async def on_guild_thread_update(event: hikari.GuildThreadUpdateEvent) -> None:
        """Handle forum thread updates (for initial post content)."""
        # Only process if this might be a new forum post getting its initial message
        if not event.thread.type == hikari.ChannelType.GUILD_PUBLIC_THREAD:
            return

        # Check if parent is a forum channel
        try:
            parent_channel = bot.cache.get_guild_channel(event.thread.parent_id)
            if not parent_channel or not is_forum_channel(parent_channel):
                return
        except:
            return

        # This could be when the initial message is added to a forum thread
        # For now, we'll skip this to avoid duplicate processing
        # The thread creation event should handle most cases

    @bot.listen()
    async def on_member_remove(event: hikari.MemberDeleteEvent) -> None:
        """Cleanup user data when they leave a guild.

        Removes squad memberships and bytes balance for the user in the guild.
        Also logs the event to the audit log if configured.
        """
        # Log to audit channel
        from smarter_dev.bot.audit_logger import log_member_leave
        try:
            await log_member_leave(bot, event)
        except Exception as e:
            logger.error(f"Failed to log member leave to audit log: {e}")

        try:
            guild_id = str(getattr(event, "guild_id", ""))
            user_id = str(getattr(event, "user_id", ""))
        except Exception:
            return

        if not guild_id or not user_id:
            return

        api_client = getattr(bot, "d", {}).get("api_client")
        if not api_client:
            logger.warning("API client not available; cannot cleanup user data on leave")
            return

        try:
            await api_client.delete(f"/guilds/{guild_id}/members/{user_id}")
            logger.info(f"Cleaned up member data for user {user_id} in guild {guild_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup member data for user {user_id} in guild {guild_id}: {e}")

    @bot.listen()
    async def on_member_join(event: hikari.MemberCreateEvent) -> None:
        """Cleanup stale user data when they join a guild.

        If the user previously left while the bot was offline, they may have stale
        squad memberships or bytes balance in the database. Clean them up to ensure
        a fresh start on next interaction.
        Also logs the event to the audit log if configured.
        """
        # Log to audit channel
        from smarter_dev.bot.audit_logger import log_member_join
        try:
            await log_member_join(bot, event)
        except Exception as e:
            logger.error(f"Failed to log member join to audit log: {e}")

        try:
            guild_id = str(getattr(event, "guild_id", ""))
            user_id = str(getattr(event, "user_id", ""))
        except Exception:
            return

        if not guild_id or not user_id:
            return

        api_client = getattr(bot, "d", {}).get("api_client")
        if not api_client:
            logger.warning("API client not available; cannot cleanup stale user data on join")
            return

        try:
            await api_client.delete(f"/guilds/{guild_id}/members/{user_id}")
            logger.info(f"Cleaned up stale data for user {user_id} joining guild {guild_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup stale data for user {user_id} in guild {guild_id}: {e}")

    # Audit log event listeners
    @bot.listen()
    async def on_ban_create(event: hikari.BanCreateEvent) -> None:
        """Log member ban events to audit log."""
        from smarter_dev.bot.audit_logger import log_member_ban
        try:
            await log_member_ban(bot, event)
        except Exception as e:
            logger.error(f"Failed to log member ban to audit log: {e}")

    @bot.listen()
    async def on_ban_delete(event: hikari.BanDeleteEvent) -> None:
        """Log member unban events to audit log."""
        from smarter_dev.bot.audit_logger import log_member_unban
        try:
            await log_member_unban(bot, event)
        except Exception as e:
            logger.error(f"Failed to log member unban to audit log: {e}")

    @bot.listen()
    async def on_message_update(event: hikari.GuildMessageUpdateEvent) -> None:
        """Log message edit events to audit log."""
        from smarter_dev.bot.audit_logger import log_message_edit
        try:
            await log_message_edit(bot, event)
        except Exception as e:
            logger.error(f"Failed to log message edit to audit log: {e}")

    @bot.listen()
    async def on_message_delete(event: hikari.GuildMessageDeleteEvent) -> None:
        """Log message delete events to audit log."""
        from smarter_dev.bot.audit_logger import log_message_delete
        try:
            await log_message_delete(bot, event)
        except Exception as e:
            logger.error(f"Failed to log message delete to audit log: {e}")

    @bot.listen()
    async def on_member_update(event: hikari.MemberUpdateEvent) -> None:
        """Log member update events (username, nickname, role changes) to audit log."""
        from smarter_dev.bot.audit_logger import log_member_update
        try:
            await log_member_update(bot, event)
        except Exception as e:
            logger.error(f"Failed to log member update to audit log: {e}")

    # Set up services before starting the bot
    logger.info("Setting up bot services...")
    await setup_bot_services(bot)
    logger.info("Bot services setup complete")
    
    # Load plugins after services are ready
    logger.info("Loading bot plugins...")
    load_plugins(bot)

    # Run bot and keep alive
    try:
        # Start the bot and wait for it to be ready
        await bot.start()

        # Keep the bot running until interrupted
        logger.info("Bot is now running. Press Ctrl+C to stop.")

        # Wait forever or until interrupted
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("Bot shutdown requested")

    except KeyboardInterrupt:
        logger.info("Bot shutdown requested via keyboard interrupt")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise
    finally:
        logger.info("Shutting down bot...")
        await bot.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot())
