"""Discord routing for the chat agent.

Responsibilities:
- Detect activation triggers: @mention or reply to a bot message.
- Run the regex stop heuristic; if triggered, kill any active engine and
  put the channel on cooldown.
- Hand activations and follow-up messages to the per-channel
  ``ChannelEngine`` via the registry.
- Increment the idle-message counter for memory staleness when the engine
  is *not* active in the channel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import hikari
import lightbulb

from smarter_dev.bot.services.chat_engine_registry import get_chat_engine_registry
from smarter_dev.bot.services.chat_memory import get_chat_memory
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.bot.utils.stop_detection import (
    is_channel_on_cooldown,
    is_stop_request,
    random_stop_ack,
    set_channel_cooldown,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("mention")


async def _voice_send(
    channel_id: int,
    text: str,
    reply_to: int | None,
    instruction: str | None = None,
) -> None:
    """Send ``text`` as a voice message via the bot's voice service.

    ``instruction`` (if provided by the agent) is a natural-language stage
    direction passed to Gemini TTS to shape tone / pacing / emotion.
    """
    voice_service = getattr(plugin.bot, "d", {}).get("voice_service") or (
        getattr(plugin.bot, "d", {}).get("_services", {}).get("voice_service")
    )
    if voice_service is None:
        logger.warning("voice_service not registered — cannot send voice message")
        await plugin.bot.rest.create_message(
            channel_id,
            text[:2000],
            reply=reply_to if reply_to else hikari.UNDEFINED,
        )
        return
    await voice_service.synthesize_and_send(
        bot=plugin.bot,
        channel_id=channel_id,
        text=text,
        reply_to_message_id=reply_to,
        instruction=instruction,
    )


def _bot_was_engaged(event: hikari.MessageCreateEvent, bot_user_id: int) -> bool:
    """True if the message @mentions the bot or replies to one of its messages."""
    if event.message.user_mentions_ids and bot_user_id in event.message.user_mentions_ids:
        return True
    ref = event.message.referenced_message
    if ref is not None and ref.author and ref.author.id == bot_user_id:
        return True
    return False


@plugin.listener(hikari.MessageCreateEvent)
async def on_message_create(event: hikari.MessageCreateEvent) -> None:
    """Route every channel message: maybe activate, maybe observe, maybe ignore."""
    if event.message.author.is_bot:
        return
    if not event.guild_id:
        return

    bot_user = plugin.bot.get_me()
    if not bot_user:
        return

    registry = get_chat_engine_registry()
    memory = get_chat_memory()

    engaged = _bot_was_engaged(event, bot_user.id)
    content = event.content or ""

    # Stop heuristic — applies to engagements and any message in an active channel.
    if engaged or await registry.has_active(event.channel_id):
        if is_stop_request(content):
            engine = await registry.get(event.channel_id)
            had_engine = engine is not None and engine.active
            if engine is not None:
                await engine.shutdown()
            set_channel_cooldown(event.channel_id)
            ack = random_stop_ack(had_engine)
            try:
                await plugin.bot.rest.create_message(
                    event.channel_id, ack, reply=event.message
                )
            except Exception:
                logger.exception("Failed to send stop ack")
            return

    if engaged:
        if is_channel_on_cooldown(event.channel_id):
            logger.info(
                "Channel %s on cooldown — ignoring engagement", event.channel_id
            )
            return
        if not rate_limiter.check_token_limit():
            logger.warning("Rate limited — refusing engagement in channel %s", event.channel_id)
            try:
                await plugin.bot.rest.create_message(
                    event.channel_id,
                    "I'm at capacity right now — try me again in a few minutes.",
                    reply=event.message,
                )
            except Exception:
                logger.exception("Failed to send rate-limit message")
            return

        # Distinguish "engine doesn't exist yet" (first activation in this
        # engagement) from "engine is already running and the user is
        # @mentioning again" — the two need different plumbing.
        existing_active = await registry.has_active(event.channel_id)
        engine = await registry.ensure_engine(
            bot=plugin.bot,
            channel_id=event.channel_id,
            guild_id=event.guild_id,
            voice_send=_voice_send,
        )
        if not existing_active:
            # Brand new engagement: kick off the initial activation. The
            # engine reads the channel history and decides for itself
            # whether to reply with voice.
            engine.trigger_initial(event.message)
        else:
            # Engine already active — enqueue this @mention/reply and
            # force-fire so the agent reacts to it right now rather than
            # waiting on the 5s idle timer.
            await engine.observe(event)
            engine.fire_now()
        return

    # Not an engagement — feed to engine if active, otherwise just bump the
    # idle-message counter so memory can detect staleness on the next activation.
    engine = await registry.get(event.channel_id)
    if engine is not None and engine.active:
        await engine.observe(event)
        return

    try:
        await memory.increment_idle_counter(event.channel_id)
    except Exception:
        logger.exception("Failed to bump idle counter for channel %s", event.channel_id)


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)
    logger.info("Mention plugin loaded (chat agent pipeline)")


def unload(bot: lightbulb.BotApp) -> None:
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(get_chat_engine_registry().shutdown_all())
    except Exception:
        logger.exception("Error shutting down chat engine registry")

    bot.remove_plugin(plugin)
    logger.info("Mention plugin unloaded")
