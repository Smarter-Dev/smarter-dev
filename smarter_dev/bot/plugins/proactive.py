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
from datetime import UTC, datetime, timedelta

import hikari
import lightbulb

from smarter_dev.bot.agents.response_fitting import (
    SUMMARIZE_THRESHOLD,
    fit_writer_message,
    split_for_discord,
)
from smarter_dev.bot.plugins.admin_gate import deny_if_not_admin
from smarter_dev.bot.proactive.adapter import TwoPassAdapter
from smarter_dev.bot.proactive.agent import (
    OPERATING_POLICY_BRIEF,
    KimiAgentRunner,
    build_agent_system_prompt,
)
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.proactive.notifications import (
    NotificationQueue,
    instruction_expired_notification,
    mode_change_notification,
    recovery_notification,
)
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
from smarter_dev.bot.services.chat_memory import get_chat_memory
from smarter_dev.bot.services.proactive_settings_service import (
    ProactiveSettingsService,
)

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("proactive")

ADMIN_DENIAL_MESSAGE = "The /proactive command is limited to server admins."
# How long a channel stays in active ingest (fast 15s/60s debounce) after a
# member engages the bot; outside it, messages wait for the 15-min sweep.
ACTIVE_WINDOW_SECONDS = 600
# How often the guild/channel memory bundle is re-read and injected into the
# agent's brief; the refresh runs lazily on the next wake after expiry.
MEMORY_REFRESH_SECONDS = 3600
# Restart recovery: how far back a startup catch-up wake may reach, and how
# many missed messages it will at most replay.
CATCHUP_MAX_AGE_SECONDS = 3600
CATCHUP_MAX_MESSAGES = 50
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


async def dispatch_response(
    bot, *, channel_id: int, content: str, reply_to_id: str | None
) -> int:
    """Send one agent response, honoring Discord's length cap.

    Mirrors the chat engine: up to SUMMARIZE_THRESHOLD the text goes out as
    at most two messages split at a newline; beyond that (the send tools
    normally refuse first) the shared summarizer condenses it rather than
    letting the tail be dropped. The reply anchor rides the first message
    only. Returns how many messages were actually sent.
    """
    if len(content) > SUMMARIZE_THRESHOLD:
        fit = await fit_writer_message(content)
        logger.info(
            "proactive overlong reply (%d chars) fitted via %s to %d chars",
            len(content), fit.method, len(fit.text),
        )
        content = fit.text
    parts = split_for_discord(content)
    reply_target = (
        int(reply_to_id) if reply_to_id and reply_to_id.isdigit() else None
    )
    sent = 0
    for index, part in enumerate(parts):
        kwargs = {}
        if index == 0 and reply_target is not None:
            kwargs["reply"] = reply_target
        try:
            await bot.rest.create_message(channel_id, part, **kwargs)
        except Exception:  # noqa: BLE001 — one failed part must not kill the wake
            logger.exception("proactive send failed")
            break
        sent += 1
    return sent


def render_memory_block(
    *, long_term_memory, long_term_updated_at, notes, topic, channel_notes
) -> str:
    """The memory bundle as one brief-ready block; empty when nothing is known."""
    sections = []
    if long_term_memory:
        stamp = (
            f" (dreamed {long_term_updated_at:%Y-%m-%d})"
            if long_term_updated_at
            else ""
        )
        sections.append(f"GUILD MEMORY{stamp}:\n{long_term_memory}")
    if notes:
        note_lines = "\n".join(
            f"- [{note.channel_name or note.channel_id or 'somewhere'}] {note.text}"
            for note in notes
        )
        sections.append(f"NOTES YOU KEPT TODAY:\n{note_lines}")
    if topic:
        sections.append(f"CHANNEL TOPIC (your earlier summary): {topic}")
    if channel_notes:
        sections.append(f"CHANNEL NOTES: {channel_notes}")
    if not sections:
        return ""
    return "YOUR MEMORY (refreshed at most hourly):\n" + "\n\n".join(sections)


async def load_memory_block(run: "ProactiveRuntime", state: "ChannelWatchState") -> str:
    """Read the same memory the chat bot injects; empty string on any failure."""
    long_term = None
    long_term_at = None
    kept_notes = ()
    guild_service = run.bot.d.get("guild_chat_memory_service")
    if guild_service is not None:
        try:
            snapshot = await guild_service.load_snapshot(state.guild_id)
            long_term = snapshot.long_term_memory
            long_term_at = snapshot.updated_at
            kept_notes = snapshot.notes
        except Exception:  # noqa: BLE001 — memory is best-effort context
            logger.warning("proactive guild memory read failed", exc_info=True)
    topic = None
    channel_notes = None
    try:
        chat_memory = get_chat_memory()
        topic_value = await chat_memory.topic_for_activation(int(state.channel_id))
        topic = topic_value.text if hasattr(topic_value, "text") else topic_value
        channel_notes = await chat_memory.get_notes(int(state.channel_id))
    except Exception:  # noqa: BLE001 — memory is best-effort context
        logger.warning("proactive channel memory read failed", exc_info=True)
    return render_memory_block(
        long_term_memory=long_term,
        long_term_updated_at=long_term_at,
        notes=kept_notes,
        topic=topic,
        channel_notes=channel_notes,
    )


def channel_message_from_hikari(message) -> ChannelMessage:
    """Convert a hikari message (or a test stub) to the shared shape."""
    author = message.author
    member = getattr(message, "member", None)
    nickname = getattr(member, "nickname", None) if member else None
    role_names: tuple[str, ...] = ()
    get_roles = getattr(member, "get_roles", None) if member else None
    if callable(get_roles):
        try:
            role_names = tuple(role.name for role in get_roles())
        except Exception:  # noqa: BLE001 — roles are metadata, never load-bearing
            role_names = ()
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
        roles=role_names,
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
    memory_refreshed_at: float = 0.0
    queue: NotificationQueue = field(default_factory=NotificationQueue)


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
        self.recovery_task: asyncio.Task | None = None

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
        instruction_store = InstructionStore.from_stored(
            OPERATING_POLICY_BRIEF, settings.watch_addendum
        )
        persisted_updates = instruction_store.updates
        for expired in instruction_store.prune_expired():
            state.queue.push(
                instruction_expired_notification(
                    instruction_id=expired.instruction_id,
                    text=expired.text,
                    created_at=datetime.now(UTC),
                )
            )

        runner = run.agent_runner_for(state)
        history_store = run.history_store()
        if history_store is not None and not state.history_loaded:
            try:
                runner.history = await history_store.read(int(state.channel_id))
            except Exception:  # noqa: BLE001 — stored history is a cache
                logger.exception("failed to load proactive history")
            state.history_loaded = True

        brief_preamble = ""
        now = time.monotonic()
        if now - state.memory_refreshed_at >= MEMORY_REFRESH_SECONDS:
            brief_preamble = await load_memory_block(run, state)
            state.memory_refreshed_at = now

        wake_deps: list[ProactiveDeps] = []

        def request_mode(mode: str, minutes: int) -> str:
            if mode == "active":
                state.active_until = time.monotonic() + max(1, minutes) * 60
                until = datetime.now(UTC) + timedelta(minutes=max(1, minutes))
                confirmation = (
                    f"Monitoring mode set to active for {minutes} minutes."
                )
            else:
                state.active_until = 0.0
                until = None
                confirmation = "Monitoring mode set to passive."
            state.queue.push(
                mode_change_notification(
                    mode=mode,
                    cause="you requested it",
                    until=until,
                    created_at=datetime.now(UTC),
                )
            )
            return confirmation

        def deps_factory(**kwargs):
            deps = ProactiveDeps(
                bot=run.bot,
                channel_id=int(state.channel_id),
                guild_id=int(state.guild_id),
                channel_name=str(state.channel_id),
                request_mode=request_mode,
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
            bot_display_name=me.username if me else "the bot",
            deps_factory=deps_factory,
            brief_preamble=brief_preamble,
            notification_queue=state.queue,
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
            await dispatch_response(
                run.bot,
                channel_id=int(state.channel_id),
                content=response.content,
                reply_to_id=response.reply_to_id,
            )
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
        if (
            history_store is not None
            and new_messages
            and new_messages[-1].id.isdigit()
        ):
            try:
                await history_store.write_cursor(
                    int(state.channel_id),
                    guild_id=state.guild_id,
                    last_message_id=new_messages[-1].id,
                )
            except Exception:  # noqa: BLE001 — the cursor is best-effort
                logger.exception("failed to persist proactive cursor")
        if instruction_store.updates != persisted_updates:
            try:
                await service.set_watch_addendum(
                    state.guild_id, state.channel_id,
                    instruction_store.to_stored(),
                )
            except Exception:  # noqa: BLE001 — persistence is best-effort
                logger.exception("failed to persist watch instructions")
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
        if now >= state.active_until:
            state.queue.push(
                mode_change_notification(
                    mode="active",
                    cause=f"{event.author.username} engaged the bot",
                    until=datetime.now(UTC)
                    + timedelta(seconds=ACTIVE_WINDOW_SECONDS),
                    created_at=datetime.now(UTC),
                )
            )
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


async def _fetch_missed(
    bot, channel_id: int, last_message_id: str
) -> list[ChannelMessage]:
    """Human messages sent after the cursor, capped by age and count."""
    fetched = []
    try:
        async for message in bot.rest.fetch_messages(
            int(channel_id), after=int(last_message_id)
        ).limit(CATCHUP_MAX_MESSAGES):
            fetched.append(message)
    except Exception:  # noqa: BLE001 — catch-up is best-effort
        logger.exception("proactive catch-up fetch failed channel=%s", channel_id)
        return []
    fetched.sort(key=lambda message: int(message.id))
    cutoff = datetime.now(UTC) - timedelta(seconds=CATCHUP_MAX_AGE_SECONDS)
    converted = [channel_message_from_hikari(m) for m in fetched]
    return [m for m in converted if not m.is_bot and m.timestamp >= cutoff]


async def _recover_channels(run: ProactiveRuntime) -> None:
    """Replay what enabled channels received while the bot was down."""
    store = run.history_store()
    service = run.settings_service()
    if store is None or service is None:
        return
    for channel_id in await store.cursor_channel_ids():
        try:
            cursor = await store.read_cursor(channel_id)
            if not cursor:
                continue
            settings = await service.get_settings(
                cursor["guild_id"], str(channel_id)
            )
            if not settings.enabled:
                continue
            missed = await _fetch_missed(
                run.bot, channel_id, cursor["last_message_id"]
            )
            if not missed:
                continue
            state = run.state_for(int(cursor["guild_id"]), channel_id)
            state.buffer.extend(missed)
            state.first_at = state.last_at = time.monotonic()
            state.queue.push(
                recovery_notification(
                    missed_count=len(missed), created_at=datetime.now(UTC)
                )
            )
            logger.info(
                "proactive recovery: %d missed messages in channel %s",
                len(missed), channel_id,
            )
            await run_wake(state)
        except Exception:  # noqa: BLE001 — one channel must not kill recovery
            logger.exception("proactive recovery failed channel=%s", channel_id)


@plugin.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    run = runtime
    if run is not None and run.passive_task is None:
        run.passive_task = asyncio.create_task(_passive_ticker())
        run.recovery_task = asyncio.create_task(_recover_channels(run))


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
    store = InstructionStore.from_stored(
        OPERATING_POLICY_BRIEF, settings.watch_addendum
    )
    if store.entries:
        instructions = "\n".join(
            f"• `{e.instruction_id}` (expires {e.expires_at:%H:%M} UTC): {e.text}"
            for e in store.entries
        )
    else:
        instructions = "(none set)"
    state = _runtime().states.get(ctx.channel_id)
    mode = (
        "active"
        if state is not None and time.monotonic() < state.active_until
        else "passive"
    )
    await ctx.respond(
        f"Proactive bot: **{'on' if settings.enabled else 'off'}** "
        f"(monitoring: {mode})\n"
        f"Watch instructions:\n{instructions[:1500]}",
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
