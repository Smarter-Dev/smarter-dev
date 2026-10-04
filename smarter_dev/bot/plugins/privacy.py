"""Privacy purge consumer (#79): starts once this process acts.

The blocked-users refresh starts with the bot's services (``client.py``); this
plugin runs the ``privacy:v1:purge`` consumer, which rewrites the chat and
embedded proactive agents' memory without one person. See
``smarter_dev.bot.privacy.purge``.
"""

from __future__ import annotations

import asyncio
import logging

import hikari
import lightbulb

from smarter_dev.bot.privacy.purge import PurgeDeps
from smarter_dev.bot.privacy.purge import purge_consumer_loop
from smarter_dev.bot.privacy.purge import supervise

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("privacy")
_task: asyncio.Task | None = None


def build_purge_deps(bot) -> PurgeDeps | None:
    """Wire the live bot into the purge; None without Redis or the bot API."""
    from smarter_dev.bot.agents.chat_compaction import _build_summarizer_model
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.proactive.models import build_twopass_model
    from smarter_dev.bot.services.chat_engine_registry import get_chat_engine_registry
    from smarter_dev.bot.services.chat_memory import ChatMemory

    redis_client = bot.d.get("chat_memory_redis")
    privacy_service = bot.d.get("privacy_service")
    if redis_client is None or privacy_service is None:
        return None

    def channel_guild(channel_id: int) -> str | None:
        channel = bot.cache.get_guild_channel(channel_id) or bot.cache.get_thread(
            channel_id
        )
        guild_id = getattr(channel, "guild_id", None)
        return str(guild_id) if guild_id is not None else None

    def proactive_model():
        run = proactive.runtime
        if run is None:
            run = proactive.ProactiveRuntime(bot, start_consumers=False)
        return build_twopass_model(run.agent_model_id)

    return PurgeDeps(
        redis=redis_client,
        chat_memory=ChatMemory(redis_client),
        chat_engines=get_chat_engine_registry().engines,
        chat_engine=get_chat_engine_registry().get,
        channel_guild=channel_guild,
        # Read at call time: the proactive plugin may load after this one.
        proactive=lambda: proactive.runtime,
        chat_model=_build_summarizer_model,
        proactive_model=proactive_model,
        post_ack=privacy_service.post_purge_ack,
    )


@plugin.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    global _task
    if _task is not None:
        return
    deps = build_purge_deps(plugin.bot)
    if deps is None:
        logger.warning("privacy purge consumer not started: no Redis or bot API")
        return
    # Supervised: if the loop ever dies it is restarted, never left dead.
    _task = asyncio.create_task(supervise(lambda: purge_consumer_loop(deps)))


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    bot.remove_plugin(plugin)
