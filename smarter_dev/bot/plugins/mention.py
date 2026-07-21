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
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

import hikari
import lightbulb
from redis.exceptions import RedisError

from smarter_dev.bot.services.chat_engine_registry import get_chat_engine_registry
from smarter_dev.bot.services.chat_memory import get_chat_memory
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.bot.services.user_message_limit import claim_notice_throttle
from smarter_dev.bot.services.user_message_limit import format_over_limit_notice
from smarter_dev.bot.services.user_message_limit import over_limit_status
from smarter_dev.bot.services.user_message_limit import record_directed_messages
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
):
    """Send ``text`` as a voice message via the bot's voice service.

    ``instruction`` (if provided by the agent) is a natural-language stage
    direction passed to Gemini TTS to shape tone / pacing / emotion.

    Returns the ``TTSUsage`` from the voice service on success so the engine
    can record tokens + cost on the persisted turn. Returns None when we
    fell back to a text reply (no voice service available).
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
        return None
    return await voice_service.synthesize_and_send(
        bot=plugin.bot,
        channel_id=channel_id,
        text=text,
        reply_to_message_id=reply_to,
        instruction=instruction,
    )


def _chat_limit_redis(bot: Any) -> Any | None:
    """The shared chat-memory Redis used for the per-user message limit.

    Returns None when Redis is unavailable (the limit then simply doesn't
    apply — a missing counter must never silence the bot).
    """
    data = getattr(bot, "d", None)
    if not isinstance(data, dict):
        return None
    return data.get("chat_memory_redis")


async def _reject_when_over_limit(bot: Any, event: hikari.MessageCreateEvent) -> bool:
    """True when the author is over the rolling message limit (drop the message).

    The first rejection of an over-limit episode pings the user with how long
    the counted window was and when they can try again; the throttle keeps
    every later rejection silent until the limit frees. Fail-soft: any Redis
    trouble reads as "under limit".
    """
    redis = _chat_limit_redis(bot)
    if redis is None:
        return False
    user_id = str(event.message.author.id)
    try:
        status = await over_limit_status(redis, user_id)
    except RedisError:
        logger.warning(
            "Failed to check message limit for user %s — allowing message",
            user_id,
            exc_info=True,
        )
        return False
    if status is None:
        return False

    try:
        first_rejection_of_episode = await claim_notice_throttle(
            redis, user_id, status.retry_epoch
        )
    except RedisError:
        logger.warning(
            "Failed to claim limit-notice throttle for user %s",
            user_id,
            exc_info=True,
        )
        first_rejection_of_episode = False
    if first_rejection_of_episode:
        notice = format_over_limit_notice(
            user_id, status, datetime.now(UTC).timestamp()
        )
        try:
            await bot.rest.create_message(
                event.channel_id,
                notice,
                reply=event.message,
                user_mentions=[event.message.author.id],
            )
        except Exception:
            logger.exception("Failed to send message-limit notice")
    return True


async def _record_engaged_message(bot: Any, event: hikari.MessageCreateEvent) -> None:
    """Count an @mention/reply engagement against its author's rolling limit.

    Engagements are charged here — before the agent runs — so they count even
    when the run later fails; the agent's rankings re-record the same message
    id post-run, which the sorted set dedupes. Best-effort: Redis trouble
    skips the charge rather than blocking the engagement.
    """
    redis = _chat_limit_redis(bot)
    if redis is None:
        return
    try:
        await record_directed_messages(
            redis,
            str(event.message.author.id),
            {str(event.message.id): event.message.created_at.timestamp()},
        )
    except RedisError:
        logger.warning(
            "Failed to record message-limit charge for user %s",
            event.message.author.id,
            exc_info=True,
        )


def _model_override_service(bot: Any) -> Any | None:
    """The bot's ModelOverrideService (set on ``bot.d``), or None if absent.

    Mirrors ``plugins/model_override._get_override_service`` but stays fail-soft:
    a bot without the service (or a non-dict ``bot.d`` in tests) yields None so
    the channel behaves as if it has no override.
    """
    data = getattr(bot, "d", None)
    if not isinstance(data, dict):
        return None
    service = data.get("model_override_service")
    if service is None:
        service = data.get("_services", {}).get("model_override_service")
    return service


async def _channel_auto_responds(bot: Any, event: hikari.MessageCreateEvent) -> bool:
    """True when the channel's override opts into replying to plain messages.

    Read fail-soft to match the chat runtime's override contract: a missing
    service or any lookup error degrades to "no auto-respond" (logged at warning)
    so a bad read never makes the bot answer — or stay silent — unexpectedly.
    """
    service = _model_override_service(bot)
    if service is None:
        return False
    try:
        override = await service.get_override(
            str(event.guild_id), str(event.channel_id)
        )
    except Exception:
        logger.warning(
            "Failed to read model override for channel %s — no auto-respond",
            event.channel_id,
            exc_info=True,
        )
        return False
    return bool(override and override.auto_respond)


def _bot_was_engaged(event: hikari.MessageCreateEvent, bot_user_id: int) -> bool:
    """True if the message @mentions the bot or replies to one of its messages."""
    if event.message.user_mentions_ids and bot_user_id in event.message.user_mentions_ids:
        return True
    ref = event.message.referenced_message
    if ref is not None and ref.author and ref.author.id == bot_user_id:
        return True
    return False


async def _activate_engine(registry: Any, event: hikari.MessageCreateEvent) -> None:
    """Run the shared activation pipeline for a triggering message.

    Both an @mention/reply and an auto-respond channel funnel through here so the
    two triggers cannot drift: apply the cooldown, rate-limit and per-user
    message-limit gates, charge the message against its author, then hand it to
    the channel engine — starting a fresh engagement or force-firing an
    already-active one. Callers invoke this only after deciding the message
    should activate (and after any stop phrase was already handled).
    """
    if is_channel_on_cooldown(event.channel_id):
        logger.info(
            "Channel %s on cooldown — ignoring activation", event.channel_id
        )
        return
    if not rate_limiter.check_token_limit():
        logger.warning("Rate limited — refusing activation in channel %s", event.channel_id)
        try:
            await plugin.bot.rest.create_message(
                event.channel_id,
                "I'm at capacity right now — try me again in a few minutes.",
                reply=event.message,
            )
        except Exception:
            logger.exception("Failed to send rate-limit message")
        return

    if await _reject_when_over_limit(plugin.bot, event):
        logger.info(
            "User %s over the rolling message limit — dropping activation "
            "in channel %s",
            event.message.author.id,
            event.channel_id,
        )
        return
    await _record_engaged_message(plugin.bot, event)

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


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message_create(event: hikari.GuildMessageCreateEvent) -> None:
    """Route every guild channel message: maybe activate, maybe observe, maybe ignore."""
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

    has_active = await registry.has_active(event.channel_id)

    # A channel override can opt into auto-respond, making the bot treat a plain
    # message exactly like an @mention/reply. Only meaningful when the bot wasn't
    # engaged and no engine is already running (an active engine's observe()
    # already sees every message). The override read is TTL-cached and runs only
    # after the cheap engaged/has_active checks above, keeping the hot path light.
    should_activate = engaged
    if not engaged and not has_active:
        should_activate = await _channel_auto_responds(plugin.bot, event)

    # Stop heuristic — applies to activations and any message in an active channel.
    if should_activate or has_active:
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

    if should_activate:
        await _activate_engine(registry, event)
        return

    # Not an activation — feed to engine if active, otherwise just bump the
    # idle-message counter so memory can detect staleness on the next activation.
    engine = await registry.get(event.channel_id)
    if engine is not None and engine.active:
        # An over-limit user's in-session follow-ups are dropped before the
        # agent sees them — otherwise one mention would buy unlimited answers
        # for the rest of the engagement. Only users who recently conversed
        # with the bot can be over the limit, so bystanders are never blocked.
        if await _reject_when_over_limit(plugin.bot, event):
            return
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
