"""Moderation monitor plugin — watches for role mentions and triggers AI moderation.

When a user mentions a monitored role (configured per-guild), the bot:
1. Fetches recent channel history for context
2. Runs the moderation AI agent with guild-configured instructions
3. The agent decides what action to take (warn, timeout, kick, ban, or just respond)
"""

from __future__ import annotations

import asyncio
import logging

import hikari
import lightbulb

from smarter_dev.bot.agents.moderation_agent import run_moderation_agent
from smarter_dev.bot.utils.messages import gather_message_context
from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.crud import ModerationConfigOperations
from smarter_dev.web.models import ModerationConfig

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("mod_monitor")

# In-memory cache of active guild configs: guild_id (str) -> ModerationConfig data
_guild_configs: dict[str, dict] = {}

mod_config_ops = ModerationConfigOperations()


async def _load_configs() -> None:
    """Load all active moderation configs into memory."""
    try:
        async with get_skrift_db_session_context() as session:
            configs = await mod_config_ops.get_all_active_configs(session)
            _guild_configs.clear()
            for config in configs:
                _guild_configs[config.guild_id] = {
                    "monitored_role_ids": set(config.monitored_role_ids or []),
                    "instructions": config.instructions or "",
                    "enabled_tools": config.enabled_tools or ["warn"],
                    "context_message_limit": config.context_message_limit or 25,
                    "response_channel_id": config.response_channel_id,
                }
            logger.info(f"Loaded moderation configs for {len(_guild_configs)} guild(s)")
    except Exception:
        logger.exception("Failed to load moderation configs")


async def refresh_config(guild_id: str) -> None:
    """Refresh a single guild's config from the database.

    Call this from the admin save endpoint to update the cache.
    """
    try:
        async with get_skrift_db_session_context() as session:
            config = await mod_config_ops.get_config(session, guild_id)
            if config and config.is_active:
                _guild_configs[guild_id] = {
                    "monitored_role_ids": set(config.monitored_role_ids or []),
                    "instructions": config.instructions or "",
                    "enabled_tools": config.enabled_tools or ["warn"],
                    "context_message_limit": config.context_message_limit or 25,
                    "response_channel_id": config.response_channel_id,
                }
                logger.info(f"Refreshed moderation config for guild {guild_id}")
            else:
                _guild_configs.pop(guild_id, None)
                logger.info(f"Removed moderation config for guild {guild_id} (inactive or deleted)")
    except Exception:
        logger.exception(f"Failed to refresh moderation config for guild {guild_id}")


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message_create(event: hikari.GuildMessageCreateEvent) -> None:
    """Listen for messages that mention monitored roles."""
    # Ignore bot messages
    if not event.message.author or event.message.author.is_bot:
        return

    guild_id = str(event.guild_id)
    config = _guild_configs.get(guild_id)
    if not config:
        return

    # Check if any monitored role was mentioned
    mentioned_role_ids = {str(r) for r in (event.message.role_mention_ids or [])}
    if not mentioned_role_ids & config["monitored_role_ids"]:
        return

    logger.info(
        f"Moderation trigger in guild {guild_id}, channel {event.channel_id} "
        f"by {event.message.author.username}: {event.message.content[:80]}..."
    )

    # Run moderation in background to not block the event loop
    asyncio.create_task(
        _handle_moderation(event, config),
        name=f"mod_monitor:{event.message.id}",
    )


async def _handle_moderation(
    event: hikari.GuildMessageCreateEvent,
    config: dict,
) -> None:
    """Handle a moderation trigger by gathering context and running the agent."""
    try:
        guild_id = str(event.guild_id)
        channel_id = str(event.channel_id)
        bot = plugin.bot

        # Gather channel history for context
        context_messages = await gather_message_context(
            bot,
            int(channel_id),
            guild_id=int(guild_id),
            limit=config["context_message_limit"],
        )

        # Run the moderation agent
        assessment = await run_moderation_agent(
            bot=bot,
            guild_id=guild_id,
            channel_id=config.get("response_channel_id") or channel_id,
            trigger_message_content=event.message.content or "",
            trigger_author=event.message.author.username,
            context_messages=context_messages,
            guild_instructions=config["instructions"],
            enabled_tools=config["enabled_tools"],
            trigger_message_id=str(event.message.id),
        )

        logger.info(f"Moderation assessment for guild {guild_id}: {assessment[:200]}")

    except Exception:
        logger.exception(
            f"Moderation handling failed for message {event.message.id} "
            f"in guild {event.guild_id}"
        )


def load(bot: lightbulb.BotApp) -> None:
    """Load the mod monitor plugin."""
    bot.add_plugin(plugin)
    # Load configs on startup
    asyncio.get_event_loop().create_task(_load_configs())


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mod monitor plugin."""
    bot.remove_plugin(plugin)
