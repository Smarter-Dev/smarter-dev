"""Discord bot client setup and configuration."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional, Set

import hikari
import lightbulb
from dataclasses import dataclass
from typing import List, Optional

from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

# Cache to track users who have already claimed their daily reward today
# Format: {f"{guild_id}:{user_id}": "YYYY-MM-DD"}
daily_claim_cache: dict[str, str] = {}


@dataclass
class ForumPostData:
    """Data structure for forum post information."""
    title: str
    content: str
    author_display_name: str
    tags: List[str]
    attachments: List[str]
    channel_id: str
    thread_id: str
    guild_id: str


def get_utc_date_string() -> str:
    """Get current UTC date as YYYY-MM-DD string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
        
        logger.info(f"✅ Guild configuration initialization complete")
        
    except Exception as e:
        logger.error(f"Failed to initialize guild configurations: {e}")


def create_bot(settings: Optional[Settings] = None) -> lightbulb.BotApp:
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
        from smarter_dev.bot.services.squads_service import SquadsService
        from smarter_dev.bot.services.forum_agent_service import ForumAgentService
        
        bytes_service = BytesService(api_client, cache_manager)
        squads_service = SquadsService(api_client, cache_manager)
        forum_agent_service = ForumAgentService(api_client, cache_manager)
        
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
        
        # Verify service health
        logger.info("Verifying service health...")
        try:
            bytes_health = await bytes_service.health_check()
            squads_health = await squads_service.health_check()
            forum_agent_health = await forum_agent_service.health_check()
            
            logger.info(f"Bytes service health: {bytes_health.status}")
            logger.info(f"Squads service health: {squads_health.status}")
            logger.info(f"Forum agent service health: {forum_agent_health.status}")
            
            if bytes_health.status != "healthy":
                logger.warning(f"Bytes service not healthy: {bytes_health.details}")
            if squads_health.status != "healthy":
                logger.warning(f"Squads service not healthy: {squads_health.details}")
            if forum_agent_health.status != "healthy":
                logger.warning(f"Forum agent service not healthy: {forum_agent_health.details}")
                
        except Exception as e:
            logger.error(f"Failed to check service health: {e}")
        
        # Store services in bot data
        if not hasattr(bot, 'd'):
            bot.d = {}
        
        bot.d['api_client'] = api_client
        bot.d['cache_manager'] = cache_manager
        bot.d['bytes_service'] = bytes_service
        bot.d['squads_service'] = squads_service
        bot.d['forum_agent_service'] = forum_agent_service
        
        # Store services in d for plugin access (primary)
        bot.d['_services'] = {
            'bytes_service': bytes_service,
            'squads_service': squads_service,
            'forum_agent_service': forum_agent_service
        }
        
        logger.info("✓ Bot services setup complete")
        logger.info(f"Services available: {list(bot.d.keys())}")
        logger.info(f"Plugin services: {list(bot.d['_services'].keys())}")
        
    except Exception as e:
        logger.error(f"Failed to setup bot services: {e}")
        # Set empty services to prevent crashes
        if not hasattr(bot, 'd'):
            bot.d = {}
        bot.d['_services'] = {}


def is_forum_channel(channel) -> bool:
    """Check if a channel is a forum channel.
    
    Args:
        channel: Discord channel object
        
    Returns:
        True if channel is a forum channel
    """
    return hasattr(channel, 'type') and channel.type == hikari.ChannelType.GUILD_FORUM


def extract_forum_post_data(thread, initial_message=None) -> ForumPostData:
    """Extract forum post data from Discord thread and message objects.
    
    Args:
        thread: Discord thread object
        initial_message: Initial message in the thread (forum post content)
        
    Returns:
        ForumPostData object with extracted information
    """
    # Extract basic information
    title = getattr(thread, 'name', '')
    thread_id = str(getattr(thread, 'id', ''))
    channel_id = str(getattr(thread, 'parent_id', ''))
    
    # Extract message information if available
    if initial_message:
        content = getattr(initial_message, 'content', '')
        author = getattr(initial_message, 'author', None)
        author_name = getattr(author, 'display_name', getattr(author, 'username', 'Unknown')) if author else 'Unknown'
        
        # Extract attachments
        attachments = []
        if hasattr(initial_message, 'attachments'):
            attachments = [getattr(att, 'filename', 'unknown') for att in initial_message.attachments]
    else:
        content = ''
        author_name = 'Unknown'
        attachments = []
    
    # Extract tags if available
    tags = []
    if hasattr(thread, 'applied_tags'):
        tags = [getattr(tag, 'name', '') for tag in thread.applied_tags if hasattr(tag, 'name')]
    
    return ForumPostData(
        title=title,
        content=content,
        author_display_name=author_name,
        tags=tags,
        attachments=attachments,
        channel_id=channel_id,
        thread_id=thread_id,
        guild_id=''  # Will be set by caller
    )


async def post_agent_responses(bot: lightbulb.BotApp, thread_id: int, responses: List[dict]) -> None:
    """Post AI agent responses to a Discord thread.
    
    Args:
        bot: Discord bot instance
        thread_id: Thread ID to post responses to
        responses: List of agent response dictionaries
    """
    if not responses:
        return
    
    try:
        for response_data in responses:
            # Only post if agent decided to respond
            if not response_data.get('should_respond', False):
                continue
                
            response_content = response_data.get('response_content', '').strip()
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
            
    except Exception as e:
        logger.error(f"Error posting agent responses to thread {thread_id}: {e}")


async def handle_forum_thread_create(bot: lightbulb.BotApp, event) -> None:
    """Handle forum thread creation events for AI agent processing.
    
    Args:
        bot: Discord bot instance
        event: Thread creation event
    """
    logger.info(f"DEBUG: handle_forum_thread_create called for thread {event.thread.id}")
    
    # Check if this is a forum thread
    if not getattr(event, 'is_forum_thread', True):
        logger.info(f"DEBUG: Not a forum thread, skipping")
        return
    
    # Check if we have a guild context
    if not hasattr(event, 'guild_id') or not event.guild_id:
        return
    
    # Get forum agent service
    forum_agent_service = getattr(bot, 'd', {}).get('forum_agent_service')
    if not forum_agent_service:
        forum_agent_service = getattr(bot, 'd', {}).get('_services', {}).get('forum_agent_service')
    
    if not forum_agent_service:
        logger.debug("No forum agent service available for thread creation")
        return
    
    try:
        # Fetch the initial message (forum post content)
        initial_message = None
        try:
            # Get the first message in the thread (the forum post)
            messages = await bot.rest.fetch_messages(event.thread.id)
            if messages:
                initial_message = messages[0]
        except Exception as e:
            logger.debug(f"Could not fetch initial message for thread {event.thread.id}: {e}")
        
        # Extract post data from the thread and initial message
        post_data = extract_forum_post_data(event.thread, initial_message)
        post_data.guild_id = str(event.guild_id)
        
        # Process the post through all applicable agents
        responses = await forum_agent_service.process_forum_post(str(event.guild_id), post_data)
        
        # Post responses that should be posted
        if responses:
            await post_agent_responses(bot, event.thread.id, responses)
            
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
        # Clean up cache manager (if used)
        if hasattr(bot, 'd') and 'cache_manager' in bot.d and bot.d['cache_manager']:
            await bot.d['cache_manager'].cleanup()
        
        # Clean up API client
        if hasattr(bot, 'd') and 'api_client' in bot.d:
            await bot.d['api_client'].close()
        
        logger.info("Bot services cleanup complete")
        
    except Exception as e:
        logger.error(f"Error cleaning up bot services: {e}")


def load_plugins(bot: lightbulb.BotApp) -> None:
    """Load bot plugins using Lightbulb v2 syntax."""
    try:
        # Check if services are available before loading plugins
        if hasattr(bot, 'd') and '_services' in bot.d:
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
        
        # Load LLM features plugin
        logger.info("Loading LLM plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.llm")
        logger.info("✓ Loaded LLM plugin")
        
        # Load events plugin for component interactions
        logger.info("Loading events plugin...")
        bot.load_extensions("smarter_dev.bot.plugins.events")
        logger.info("✓ Loaded events plugin")
        
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
        bytes_service = getattr(bot, 'd', {}).get('bytes_service')
        if not bytes_service:
            bytes_service = getattr(bot, 'd', {}).get('_services', {}).get('bytes_service')
        
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
                
                # Add reaction to the message that earned bytes
                try:
                    await event.message.add_reaction("🎉")
                    logger.info(f"✅ Added reaction and awarded daily bytes reward ({result.earned}) to {event.author}")
                except Exception as e:
                    logger.error(f"Failed to add reaction to daily reward message: {e}")
            else:
                logger.debug(f"Daily reward not successful for {event.author}")
                
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
        if not hasattr(bot, 'd'):
            bot.d = {}
        if 'active_views' not in bot.d:
            bot.d['active_views'] = {}
        
        # Handle squad-related interactions
        custom_id = event.interaction.custom_id
        user_id = str(event.interaction.user.id)
        
        if custom_id in ["squad_select", "squad_confirm", "squad_cancel"]:
            logger.info(f"Received {custom_id} interaction from user {user_id}")
            
            # Check if there's an active view for this user
            view_key = f"{user_id}_{custom_id.split('_')[0]}"  # user_id_squad
            active_view = bot.d['active_views'].get(view_key)
            
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
    
    # Set up services before loading plugins
    logger.info("Setting up bot services...")
    await setup_bot_services(bot)
    
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