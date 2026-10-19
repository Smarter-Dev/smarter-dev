"""Moderation monitor plugin — watches for role mentions and triggers AI triage.

When a user mentions a monitored role (configured per-guild), the bot:
1. Fetches recent channel history for context
2. Runs the moderation triage agent with guild-configured instructions
3. The agent decides what action to take (timeout, purge, flag, or just message)
4. A channel message is posted with appended user pings
5. A summary report embed is sent to the configured mod channel
"""

from __future__ import annotations

import asyncio
import logging

import hikari
import lightbulb

from smarter_dev.bot import leadership
from smarter_dev.bot.agents.mod_tools import build_triage_report_embed
from smarter_dev.bot.agents.moderation_agent import run_moderation_agent
from smarter_dev.bot.triage_config import TriageConfig
from smarter_dev.bot.triage_config import load_triage_config
from smarter_dev.bot.utils.messages import gather_message_context
from smarter_dev.shared.exception_logging import log_exception

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("mod_monitor")


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message_create(event: hikari.GuildMessageCreateEvent) -> None:
    """Listen for messages that mention monitored roles."""
    # Ignore bot messages
    if not event.message.author or event.message.author.is_bot:
        return

    mentioned_role_ids = {str(r) for r in (event.message.role_mention_ids or [])}
    if not mentioned_role_ids:
        return

    # Read the config now, not from a startup cache: a save in the admin UI
    # (which runs in the web process) must reach the very next ping. Only
    # messages that mention a role get this far, so the read is rare.
    guild_id = str(event.guild_id)
    try:
        config = await load_triage_config(guild_id)
    except Exception:
        log_exception(logger, f"Could not read moderation config for guild {guild_id}")
        return
    if config is None or not mentioned_role_ids & config.monitored_role_ids:
        return

    logger.info(
        f"Moderation trigger in guild {guild_id}, channel {event.channel_id} "
        f"by author {event.message.author.id}: message {event.message.id} "
        f"({len(event.message.content or '')} chars)"
    )

    # Run moderation in background to not block the event loop; tracked so a
    # process handing over finishes it rather than dropping it.
    leadership.track(
        asyncio.create_task(
            _handle_moderation(event, config),
            name=f"mod_monitor:{event.message.id}",
        )
    )


async def _handle_moderation(
    event: hikari.GuildMessageCreateEvent,
    config: TriageConfig,
) -> None:
    """Handle a moderation trigger by gathering context and running the triage agent."""
    try:
        guild_id = str(event.guild_id)
        channel_id = str(event.channel_id)
        bot = plugin.bot
        trigger_message_id = str(event.message.id)

        # Gather channel history for context
        context_messages = await gather_message_context(
            bot,
            int(channel_id),
            guild_id=int(guild_id),
            limit=config.context_message_limit,
        )

        # Run the triage agent — always operate in the INCIDENT channel
        assessment, tracker = await run_moderation_agent(
            bot=bot,
            guild_id=guild_id,
            channel_id=channel_id,  # incident channel, NOT response_channel_id
            trigger_message_content=event.message.content or "",
            trigger_author=event.message.author.username,
            context_messages=context_messages,
            guild_instructions=config.instructions,
            enabled_tools=list(config.enabled_tools),
            trigger_message_id=trigger_message_id,
        )

        # Post channel message with appended user pings
        if tracker.channel_message:
            impacted_ids = tracker.all_impacted_user_ids
            message_parts = [tracker.channel_message]
            if impacted_ids:
                pings = " ".join(f"<@{uid}>" for uid in sorted(impacted_ids))
                message_parts.append(f"-# {pings}")
            message_parts.append("-# Moderators have been notified")
            full_message = "\n".join(message_parts)

            try:
                await bot.rest.create_message(int(channel_id), full_message)
            except Exception:
                log_exception(logger, f"Failed to send channel message for guild {guild_id}")

        # Post triage report to mod channel
        report_channel = config.response_channel_id or channel_id
        report_embed = build_triage_report_embed(
            tracker=tracker,
            assessment=assessment,
            trigger_author=event.message.author.username,
            channel_id=channel_id,
            trigger_message_id=trigger_message_id,
        )
        try:
            await bot.rest.create_message(int(report_channel), embed=report_embed)
        except Exception:
            log_exception(logger, f"Failed to send triage report for guild {guild_id}")

        logger.info(f"Moderation triage completed for guild {guild_id}: {assessment[:200]}")

    except Exception:
        log_exception(
            logger,
            f"Moderation handling failed for message {event.message.id} "
            f"in guild {event.guild_id}"
        )


def load(bot: lightbulb.BotApp) -> None:
    """Load the mod monitor plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mod monitor plugin."""
    bot.remove_plugin(plugin)
