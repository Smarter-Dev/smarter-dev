"""Proactive chat bot plugin: passive watch, active ingest, /proactive toggle.

Two scheduling modes per enabled channel:

- PASSIVE (the default, all messages at all times): messages buffer and the
  15-minute sweep reviews each batch through the DeepSeek watcher — cold
  entries happen here, at sweep latency and minimal cost.
- ACTIVE ingest: when a member engages the bot (@mention or reply to a bot
  message) the channel switches to the fast 15s-quiet/60s-cap debounce for
  ACTIVE_WINDOW_SECONDS, so the conversation feels responsive; every
  further engagement extends the window, and it decays back to passive by
  absence.

Engagement messages wake the agent deterministically; everything else goes
through the watcher gate. The agent (Gemini 3.7 Flash by default, full
chat-tool parity) acts or deliberately stays silent; its watch-instruction
updates persist per channel via the proactive-settings API, and its history
persists in Redis.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import hikari
import lightbulb

from smarter_dev.bot.plugins.admin_gate import deny_if_not_admin
from smarter_dev.bot.proactive.adapter import TwoPassAdapter
from smarter_dev.bot.proactive.agent import (
    OPERATING_POLICY_BRIEF,
    KimiAgentRunner,
    build_agent_system_prompt,
)
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.proactive.models import (
    build_twopass_model,
    ensure_openrouter_key_alias,
    resolve_agent_model_id,
)
from smarter_dev.bot.proactive.parity import ProactiveDeps, build_proactive_agent
from smarter_dev.bot.proactive.types import ActivationContext, ChannelMessage
from smarter_dev.bot.proactive.watcher import SkimRunner, WatcherRunner
from smarter_dev.bot.proactive.windows import (
    MAX_WAIT_SECONDS,
    PASSIVE_SECONDS,
    QUIET_SECONDS,
)
from smarter_dev.bot.services.proactive_settings_service import (
    ProactiveSettingsService,
)

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("proactive")

ADMIN_DENIAL_MESSAGE = "The /proactive command is limited to server admins."
# How long a channel stays in active ingest (fast 15s/60s debounce) after a
# member engages the bot; outside it, messages wait for the 15-min sweep.
ACTIVE_WINDOW_SECONDS = 600
AGENT_MODEL_ENV_VAR = "PROACTIVE_AGENT_MODEL"
WATCHER_MODEL_ENV_VAR = "PROACTIVE_WATCHER_MODEL"
DEFAULT_AGENT_MODEL = "gemini-3.7-flash"
DEFAULT_WATCHER_MODEL = "deepseek/deepseek-v4-flash"
HISTORY_FETCH_LIMIT = 60


def compute_fire_delay(
    first_at: float,
    last_at: float,
    now: float,
    *,
    quiet_seconds: float = QUIET_SECONDS,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
) -> float:
    """Seconds until the current burst should fire, measured from ``now``."""
    fire_at = min(last_at + quiet_seconds, first_at + max_wait_seconds)
    return max(0.0, fire_at - now)


def event_engages_bot(message, bot_user_id: str) -> bool:
    """True when the message @mentions the bot or replies to a bot message."""
    mention_ids = getattr(message, "user_mentions_ids", None) or ()
    if bot_user_id in {str(mention_id) for mention_id in mention_ids}:
        return True
    referenced = getattr(message, "referenced_message", None)
    author = getattr(referenced, "author", None) if referenced else None
    return author is not None and str(author.id) == bot_user_id


def channel_message_from_hikari(message) -> ChannelMessage:
    """Convert a hikari message (or a test stub) to the shared shape."""
    author = message.author
    member = getattr(message, "member", None)
    nickname = getattr(member, "nickname", None) if member else None
    display = (
        nickname or getattr(author, "global_name", None) or author.username
    )
    referenced = getattr(message, "referenced_message", None)
    reply_to_id = str(referenced.id) if referenced else None
    mention_ids = tuple(
        str(mention_id)
        for mention_id in (getattr(message, "user_mentions_ids", None) or ())
    )
    try:
        message_type = int(message.type)
    except (TypeError, ValueError):
        message_type = 0
    return ChannelMessage(
        id=str(message.id),
        timestamp=message.created_at,
        author_id=str(author.id),
        author_name=author.username,
        author_display=display,
        is_bot=bool(author.is_bot),
        content=message.content or "",
        reply_to_id=reply_to_id,
        mention_user_ids=mention_ids,
        mention_everyone=bool(getattr(message, "mentions_everyone", False)),
        attachment_count=len(getattr(message, "attachments", ()) or ()),
        sticker_count=len(getattr(message, "stickers", ()) or ()),
        message_type=message_type,
    )


@dataclass
class ChannelWatchState:
    """Per-channel burst buffer, debounce timer and persistent agent."""

    guild_id: str
    channel_id: str
    buffer: list[ChannelMessage] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    timer: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent_runner: KimiAgentRunner | None = None
    last_wake_at: float = 0.0
    history_loaded: bool = False
    # Monotonic deadline of the active-ingest window; 0 means passive.
    active_until: float = 0.0


class ProactiveRuntime:
    """All live state: shared watcher/skim, per-channel agents and buffers."""

    def __init__(self, bot: lightbulb.BotApp):
        self.bot = bot
        self.states: dict[int, ChannelWatchState] = {}
        self._watcher: WatcherRunner | None = None
        self._skim: SkimRunner | None = None
        self._agent_model_id: str | None = None
        self._history_store: ProactiveHistoryStore | None = None
        self.passive_task: asyncio.Task | None = None

    # -- lazy model construction (env keys are only needed on first wake) --

    @property
    def watcher_model_id(self) -> str:
        return os.getenv(WATCHER_MODEL_ENV_VAR, DEFAULT_WATCHER_MODEL)

    @property
    def agent_model_id(self) -> str:
        if self._agent_model_id is None:
            ensure_openrouter_key_alias()
            self._agent_model_id = resolve_agent_model_id(
                os.getenv(AGENT_MODEL_ENV_VAR, DEFAULT_AGENT_MODEL)
            )
        return self._agent_model_id

    def watcher(self) -> WatcherRunner:
        if self._watcher is None:
            self._watcher = WatcherRunner(
                build_twopass_model(self.watcher_model_id)
            )
        return self._watcher

    def skim(self) -> SkimRunner:
        if self._skim is None:
            self._skim = SkimRunner(build_twopass_model(self.watcher_model_id))
        return self._skim

    def settings_service(self) -> ProactiveSettingsService | None:
        return self.bot.d.get("proactive_settings_service")

    def history_store(self) -> ProactiveHistoryStore | None:
        if self._history_store is None:
            redis_client = self.bot.d.get("chat_memory_redis")
            if redis_client is None:
                return None
            self._history_store = ProactiveHistoryStore(redis_client)
        return self._history_store

    def state_for(self, guild_id: int, channel_id: int) -> ChannelWatchState:
        state = self.states.get(channel_id)
        if state is None:
            state = ChannelWatchState(
                guild_id=str(guild_id), channel_id=str(channel_id)
            )
            self.states[channel_id] = state
        return state

    def agent_runner_for(self, state: ChannelWatchState) -> KimiAgentRunner:
        if state.agent_runner is None:
            channel = self.bot.cache.get_guild_channel(int(state.channel_id))
            guild = self.bot.cache.get_guild(int(state.guild_id))
            me = self.bot.get_me()

            async def compaction_summarize(text: str) -> str:
                summary, usage = await self.skim().skim(text)
                logger.info("proactive history compaction: %s", usage)
                return summary

            state.agent_runner = KimiAgentRunner(
                agent=build_proactive_agent(
                    build_twopass_model(self.agent_model_id),
                    system_prompt=build_agent_system_prompt(
                        bot_display_name=me.username if me else "smarter-bot",
                        bot_user_id=str(me.id) if me else "",
                        channel_name=(
                            getattr(channel, "name", None) or state.channel_id
                        ),
                        guild_name=(
                            getattr(guild, "name", None) or state.guild_id
                        ),
                    ),
                ),
                summarize=compaction_summarize,
            )
        return state.agent_runner


runtime: ProactiveRuntime | None = None


def _runtime() -> ProactiveRuntime:
    if runtime is None:
        raise RuntimeError("proactive plugin not loaded")
    return runtime


async def _fetch_history(
    bot, channel_id: int, exclude_ids: set[str]
) -> list[ChannelMessage]:
    fetched: list[ChannelMessage] = []
    async for message in bot.rest.fetch_messages(channel_id).limit(
        HISTORY_FETCH_LIMIT
    ):
        converted = channel_message_from_hikari(message)
        if converted.id not in exclude_ids:
            fetched.append(converted)
    fetched.reverse()
    return fetched


async def run_wake(state: ChannelWatchState, *, passive: bool = False) -> None:
    """One activation: drain the buffer, run the two-pass system, dispatch."""
    run = _runtime()
    service = run.settings_service()
    if service is None:
        return
    async with state.lock:
        new_messages = list(state.buffer)
        state.buffer.clear()
        state.last_wake_at = time.monotonic()
        if not new_messages and not passive:
            return
        settings = await service.get_settings(state.guild_id, state.channel_id)
        if not settings.enabled:
            return

        me = run.bot.get_me()
        bot_user_id = str(me.id) if me else ""
        history = await _fetch_history(
            run.bot,
            int(state.channel_id),
            exclude_ids={m.id for m in new_messages},
        )
        instruction_store = InstructionStore(seed=OPERATING_POLICY_BRIEF)
        instruction_store.addendum = settings.watch_addendum

        runner = run.agent_runner_for(state)
        history_store = run.history_store()
        if history_store is not None and not state.history_loaded:
            try:
                runner.history = await history_store.read(int(state.channel_id))
            except Exception:  # noqa: BLE001 — stored history is a cache
                logger.exception("failed to load proactive history")
            state.history_loaded = True

        wake_deps: list[ProactiveDeps] = []

        def deps_factory(**kwargs):
            deps = ProactiveDeps(
                bot=run.bot,
                channel_id=int(state.channel_id),
                guild_id=int(state.guild_id),
                channel_name=str(state.channel_id),
                **kwargs,
            )
            wake_deps.append(deps)
            return deps

        adapter = TwoPassAdapter(
            watcher=run.watcher(),
            agent_runner=runner,
            skim=run.skim(),
            instruction_store=instruction_store,
            watcher_model_id=run.watcher_model_id,
            agent_model_id=run.agent_model_id,
            deps_factory=deps_factory,
        )
        context = ActivationContext(
            channel_name=str(state.channel_id),
            guild_name=str(state.guild_id),
            bot_user_id=bot_user_id,
            activated_at=datetime.now(UTC),
            history=history,
            new_messages=new_messages,
        )
        result = await adapter.activate(context)

        for response in result.responses:
            kwargs = {}
            if response.reply_to_id and response.reply_to_id.isdigit():
                kwargs["reply"] = int(response.reply_to_id)
            try:
                await run.bot.rest.create_message(
                    int(state.channel_id), response.content[:2000], **kwargs
                )
            except Exception:  # noqa: BLE001 — one failed send must not kill the wake
                logger.exception("proactive send failed")
        for reaction in result.reactions:
            if not reaction.message_id.isdigit():
                continue
            try:
                await run.bot.rest.add_reaction(
                    int(state.channel_id), int(reaction.message_id),
                    reaction.emoji,
                )
            except Exception:  # noqa: BLE001
                logger.exception("proactive reaction failed")
        pending_images = [
            image for deps in wake_deps for image in deps.pending_images
        ]
        if pending_images:
            reply_target = next(
                (
                    int(r.reply_to_id)
                    for r in result.responses
                    if r.reply_to_id and r.reply_to_id.isdigit()
                ),
                None,
            )
            image_kwargs = {
                "attachments": [
                    hikari.Bytes(image.data, image.filename, image.mime_type)
                    for image in pending_images
                ]
            }
            if reply_target is not None:
                image_kwargs["reply"] = reply_target
            try:
                await run.bot.rest.create_message(
                    int(state.channel_id), **image_kwargs
                )
            except Exception:  # noqa: BLE001 — images are best-effort extras
                logger.exception("proactive image post failed")
        if history_store is not None:
            try:
                await history_store.write(int(state.channel_id), runner.history)
            except Exception:  # noqa: BLE001 — persistence is best-effort
                logger.exception("failed to persist proactive history")
        if instruction_store.updates:
            try:
                await service.set_watch_addendum(
                    state.guild_id, state.channel_id, instruction_store.addendum
                )
            except Exception:  # noqa: BLE001 — persistence is best-effort
                logger.exception("failed to persist watch addendum")
        logger.info(
            "proactive wake channel=%s new=%d responses=%d reactions=%d "
            "passive=%s details=%s",
            state.channel_id, len(new_messages), len(result.responses),
            len(result.reactions), passive,
            (result.details or {}).get("watcher", {}).get("wake"),
        )


def _schedule_wake(state: ChannelWatchState) -> None:
    if state.timer is not None and not state.timer.done():
        state.timer.cancel()

    async def fire_after_delay() -> None:
        delay = compute_fire_delay(
            state.first_at, state.last_at, time.monotonic()
        )
        await asyncio.sleep(delay)
        await run_wake(state)

    state.timer = asyncio.create_task(fire_after_delay())


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_guild_message(event: hikari.GuildMessageCreateEvent) -> None:
    if not event.author or event.author.is_bot or not event.guild_id:
        return
    run = runtime
    if run is None:
        return
    service = run.settings_service()
    if service is None:
        return
    try:
        settings = await service.get_settings(
            str(event.guild_id), str(event.channel_id)
        )
    except Exception:  # noqa: BLE001 — settings failure means "off", not a crash
        logger.warning("proactive settings lookup failed", exc_info=True)
        return
    if not settings.enabled:
        return
    state = run.state_for(event.guild_id, event.channel_id)
    now = time.monotonic()
    if not state.buffer:
        state.first_at = now
    state.last_at = now
    state.buffer.append(channel_message_from_hikari(event.message))

    me = run.bot.get_me()
    bot_user_id = str(me.id) if me else ""
    if event_engages_bot(event.message, bot_user_id):
        # A member engaged the bot: active ingest for a while, so the
        # conversation feels responsive.
        state.active_until = now + ACTIVE_WINDOW_SECONDS
    if now < state.active_until:
        _schedule_wake(state)
    # Passive channels leave the buffer for the 15-minute sweep.


async def _passive_sweep(run: ProactiveRuntime) -> None:
    """One 15-minute pass: drain passive buffers, revisit long-idle channels."""
    for state in list(run.states.values()):
        try:
            if state.buffer:
                await run_wake(state)
                continue
            idle_for = time.monotonic() - max(state.last_wake_at, state.last_at)
            if idle_for >= PASSIVE_SECONDS:
                await run_wake(state, passive=True)
        except Exception:  # noqa: BLE001 — one channel must not kill the ticker
            logger.exception("passive sweep failed channel=%s", state.channel_id)


async def _passive_ticker() -> None:
    while True:
        await asyncio.sleep(PASSIVE_SECONDS)
        run = runtime
        if run is None:
            return
        await _passive_sweep(run)


@plugin.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    run = runtime
    if run is not None and run.passive_task is None:
        run.passive_task = asyncio.create_task(_passive_ticker())


@plugin.command
@lightbulb.command("proactive", "Proactive chat bot for this channel (admin only)")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def proactive_group(ctx: lightbulb.Context) -> None:
    pass


async def _set_enabled(ctx: lightbulb.Context, enabled: bool) -> None:
    if await deny_if_not_admin(ctx, ADMIN_DENIAL_MESSAGE):
        return
    service = _runtime().settings_service()
    if service is None:
        await ctx.respond(
            "Proactive settings service is unavailable.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return
    await service.set_enabled(str(ctx.guild_id), str(ctx.channel_id), enabled)
    await ctx.respond(
        f"Proactive bot is now **{'on' if enabled else 'off'}** in this channel.",
        flags=hikari.MessageFlag.EPHEMERAL,
    )


@proactive_group.child
@lightbulb.command("on", "Enable the proactive bot in this channel")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def proactive_on(ctx: lightbulb.Context) -> None:
    await _set_enabled(ctx, True)


@proactive_group.child
@lightbulb.command("off", "Disable the proactive bot in this channel")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def proactive_off(ctx: lightbulb.Context) -> None:
    await _set_enabled(ctx, False)


@proactive_group.child
@lightbulb.command("status", "Show the proactive bot's status for this channel")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def proactive_status(ctx: lightbulb.Context) -> None:
    if await deny_if_not_admin(ctx, ADMIN_DENIAL_MESSAGE):
        return
    service = _runtime().settings_service()
    if service is None:
        await ctx.respond(
            "Proactive settings service is unavailable.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return
    settings = await service.get_settings(str(ctx.guild_id), str(ctx.channel_id))
    addendum = settings.watch_addendum or "(none)"
    await ctx.respond(
        f"Proactive bot: **{'on' if settings.enabled else 'off'}**\n"
        f"Watch addendum: {addendum[:1500]}",
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def load(bot: lightbulb.BotApp) -> None:
    global runtime
    runtime = ProactiveRuntime(bot)
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    global runtime
    if runtime is not None and runtime.passive_task is not None:
        runtime.passive_task.cancel()
    runtime = None
    bot.remove_plugin(plugin)
