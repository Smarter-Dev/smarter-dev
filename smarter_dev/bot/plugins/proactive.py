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
import socket
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import hikari
import lightbulb
from pydantic_ai.messages import ModelMessagesTypeAdapter

from smarter_dev.bot.agents.response_fitting import SUMMARIZE_THRESHOLD
from smarter_dev.bot.agents.response_fitting import fit_writer_message
from smarter_dev.bot.agents.response_fitting import split_for_discord
from smarter_dev.bot.plugins.admin_gate import is_admin
from smarter_dev.bot.proactive.adapter import AgentConsumer
from smarter_dev.bot.proactive.adapter import WatcherProducer
from smarter_dev.bot.proactive.adapter import bot_directed_message_ids
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.agent import KimiAgentRunner
from smarter_dev.bot.proactive.agent import build_guild_agent_system_prompt
from smarter_dev.bot.proactive.agent import self_compaction_summary
from smarter_dev.bot.proactive.contracts import ControlCommand
from smarter_dev.bot.proactive.contracts import NotificationEnvelope
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.proactive.models import build_twopass_model
from smarter_dev.bot.proactive.models import ensure_openrouter_key_alias
from smarter_dev.bot.proactive.models import resolve_agent_model_id
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.notifications import NotificationQueue
from smarter_dev.bot.proactive.notifications import channel_enabled_notification
from smarter_dev.bot.proactive.notifications import instruction_expired_notification
from smarter_dev.bot.proactive.notifications import mention_notification
from smarter_dev.bot.proactive.notifications import mode_change_notification
from smarter_dev.bot.proactive.notifications import reaction_notification
from smarter_dev.bot.proactive.notifications import recovery_notification
from smarter_dev.bot.proactive.notifications import render_notifications
from smarter_dev.bot.proactive.notifications import reply_notification
from smarter_dev.bot.proactive.parity import ProactiveDeps
from smarter_dev.bot.proactive.parity import build_proactive_agent
from smarter_dev.bot.proactive.redis_queue import RedisNotificationQueue
from smarter_dev.bot.proactive.types import ActivationContext
from smarter_dev.bot.proactive.types import ChannelMessage
from smarter_dev.bot.proactive.watcher import SkimRunner
from smarter_dev.bot.proactive.watcher import WatcherRunner
from smarter_dev.bot.proactive.windows import MAX_WAIT_SECONDS
from smarter_dev.bot.proactive.windows import PASSIVE_SECONDS
from smarter_dev.bot.proactive.windows import QUIET_SECONDS
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.proactive_settings_service import ProactiveSettingsService

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("proactive")

MODERATOR_DENIAL_MESSAGE = (
    "The /proactive command is limited to moderators — it needs the Manage "
    "Messages permission."
)
# How long a channel stays in active ingest (fast 15s/60s debounce) after a
# member engages the bot; outside it, messages wait for the 15-min sweep.
ACTIVE_WINDOW_SECONDS = 600
# Review messages buffered during startup promptly, then use the normal cadence.
FIRST_PASSIVE_SWEEP_SECONDS = 120
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
DEFAULT_WATCHER_MODEL = "z-ai/glm-5.3-flash"
HISTORY_FETCH_LIMIT = 60
# Cap on one compaction-summarize LLM call; past it the wake falls back to
# the agent model, then to truncation. A hung summarize blocks the guild's
# whole consumer loop, so this must be finite.
COMPACTION_TIMEOUT_SECONDS = 120
SETTINGS_RETRY_BACKOFF_SECONDS = 5
EXECUTION_MODE_ENV_VAR = "PROACTIVE_AGENT_EXECUTION_MODE"
EXTERNAL_GUILDS_ENV_VAR = "PROACTIVE_AGENT_EXTERNAL_GUILD_IDS"
SHADOW_GUILDS_ENV_VAR = "PROACTIVE_AGENT_SHADOW_GUILD_IDS"
EMBEDDED_GUILDS_ENV_VAR = "PROACTIVE_AGENT_EMBEDDED_GUILD_IDS"
EMBEDDED_EXECUTION_MODE = "embedded"
SHADOW_EXECUTION_MODE = "shadow"
EXTERNAL_EXECUTION_MODE = "external"
EXECUTION_MODES = {
    EMBEDDED_EXECUTION_MODE,
    SHADOW_EXECUTION_MODE,
    EXTERNAL_EXECUTION_MODE,
}
CONTROL_STREAM_KEY = "proactive:v1:control"
CONTROL_GROUP = "proactive-bot-v1"
CONTROL_PROCESSED_PREFIX = "proactive:v1:control-processed"


def _guild_id_set(raw: str) -> set[str]:
    return {value.strip() for value in raw.split(",") if value.strip()}


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


def has_moderator_permissions(permissions: hikari.Permissions) -> bool:
    """True for Manage Messages (or Administrator, which implies it).

    Deliberately looser than the Administrator gate the other admin commands
    use: a moderator who can police a channel must be able to switch the
    proactive bot off there without holding full admin.
    """
    return bool(permissions & hikari.Permissions.MANAGE_MESSAGES) or is_admin(
        permissions
    )


async def deny_without_moderator_permissions(ctx, denial_message: str) -> bool:
    """Gate a command on moderator permissions; respond ephemerally on deny."""
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond(
            "This command only works in a server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True
    if not has_moderator_permissions(lightbulb.utils.permissions_for(ctx.member)):
        await ctx.respond(denial_message, flags=hikari.MessageFlag.EPHEMERAL)
        return True
    return False


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
            len(content),
            fit.method,
            len(fit.text),
        )
        content = fit.text
    parts = split_for_discord(content)
    reply_target = int(reply_to_id) if reply_to_id and reply_to_id.isdigit() else None
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


def render_memory_block(*, long_term_memory, long_term_updated_at, notes) -> str:
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
    if not sections:
        return ""
    return "YOUR MEMORY (refreshed at most hourly):\n" + "\n\n".join(sections)


async def load_memory_block(run: ProactiveRuntime, guild_id: str) -> str:
    """Read guild memory only; per-channel reads do not scale with a guild."""
    long_term = None
    long_term_at = None
    kept_notes = ()
    guild_service = run.bot.d.get("guild_chat_memory_service")
    if guild_service is not None:
        try:
            snapshot = await guild_service.load_snapshot(guild_id)
            long_term = snapshot.long_term_memory
            long_term_at = snapshot.updated_at
            kept_notes = snapshot.notes
        except Exception:  # noqa: BLE001 — memory is best-effort context
            logger.warning("proactive guild memory read failed", exc_info=True)
    return render_memory_block(
        long_term_memory=long_term,
        long_term_updated_at=long_term_at,
        notes=kept_notes,
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
    display = nickname or getattr(author, "global_name", None) or author.username
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
class ChannelProducerState:
    """Per-channel watcher buffer and review cursor."""

    guild_id: str
    channel_id: str
    buffer: list[ChannelMessage] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    timer: asyncio.Task | None = None
    producer_tasks: set[asyncio.Task] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_wake_at: float = 0.0
    # Monotonic deadline of the active-ingest window; 0 means passive.
    active_until: float = 0.0
    # Whether the stored active window was already restored after a restart.
    active_window_restored: bool = False
    last_reviewed_message_id: str | None = None
    pending_directed_ids: set[str] = field(default_factory=set)


@dataclass
class GuildAgentState:
    """One notification consumer and persistent agent per guild."""

    guild_id: str
    queue: NotificationQueue = field(default_factory=NotificationQueue)
    consumer_task: asyncio.Task | None = None
    agent_runner: KimiAgentRunner | None = None
    history_loaded: bool = False
    memory_refreshed_at: float = 0.0
    pending_passive_wake: bool = False


def _channel_name_for_id(bot, channel_id: str) -> str:
    channel = bot.cache.get_guild_channel(int(channel_id))
    return getattr(channel, "name", None) or channel_id


def _channel_name(bot, state: ChannelProducerState) -> str:
    return _channel_name_for_id(bot, state.channel_id)


class ProactiveRuntime:
    """Shared models, per-channel producers, and per-guild agents."""

    def __init__(
        self,
        bot: lightbulb.BotApp,
        *,
        start_consumers: bool = True,
        execution_mode: str | None = None,
    ):
        self.bot = bot
        self.execution_mode = execution_mode or os.getenv(
            EXECUTION_MODE_ENV_VAR, EMBEDDED_EXECUTION_MODE
        )
        if self.execution_mode not in EXECUTION_MODES:
            raise ValueError(
                f"{EXECUTION_MODE_ENV_VAR} must be one of "
                f"{', '.join(sorted(EXECUTION_MODES))}"
            )
        self.channel_states: dict[int, ChannelProducerState] = {}
        self.guild_states: dict[int, GuildAgentState] = {}
        self._watcher: WatcherRunner | None = None
        self._skim: SkimRunner | None = None
        self._agent_model_id: str | None = None
        self._history_store: ProactiveHistoryStore | None = None
        self._redis_notification_queue: RedisNotificationQueue | None = None
        self.passive_task: asyncio.Task | None = None
        self.recovery_task: asyncio.Task | None = None
        self.control_task: asyncio.Task | None = None
        self.external_guild_ids = _guild_id_set(os.getenv(EXTERNAL_GUILDS_ENV_VAR, ""))
        self.shadow_guild_ids = _guild_id_set(os.getenv(SHADOW_GUILDS_ENV_VAR, ""))
        self.embedded_guild_ids = _guild_id_set(os.getenv(EMBEDDED_GUILDS_ENV_VAR, ""))
        selections = (
            self.external_guild_ids,
            self.shadow_guild_ids,
            self.embedded_guild_ids,
        )
        if any(
            left & right
            for i, left in enumerate(selections)
            for right in selections[i + 1 :]
        ):
            raise ValueError("proactive per-guild execution lists must not overlap")
        self.start_consumers = start_consumers

    def execution_mode_for(self, guild_id: str) -> str:
        """Resolve ownership per guild so rollout can be canaried safely."""
        if guild_id in self.embedded_guild_ids:
            return EMBEDDED_EXECUTION_MODE
        if guild_id in self.external_guild_ids:
            return EXTERNAL_EXECUTION_MODE
        if guild_id in self.shadow_guild_ids:
            return SHADOW_EXECUTION_MODE
        return self.execution_mode

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
            self._watcher = WatcherRunner(build_twopass_model(self.watcher_model_id))
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

    def redis_notification_queue(self) -> RedisNotificationQueue | None:
        if self._redis_notification_queue is None:
            redis_client = self.bot.d.get("chat_memory_redis")
            if redis_client is None:
                return None
            self._redis_notification_queue = RedisNotificationQueue(redis_client)
        return self._redis_notification_queue

    async def sync_execution_ownership(self) -> None:
        """Publish the authoritative owner for every connected/canary guild."""
        redis_queue = self.redis_notification_queue()
        guild_ids = {
            *(str(guild_id) for guild_id in self.bot.cache.get_guilds_view()),
            *self.external_guild_ids,
            *self.shadow_guild_ids,
            *self.embedded_guild_ids,
        }
        if redis_queue is None:
            external = [
                guild_id
                for guild_id in guild_ids
                if self.execution_mode_for(guild_id) == EXTERNAL_EXECUTION_MODE
            ]
            if external:
                raise RuntimeError(
                    "proactive external notification queue is unavailable"
                )
            return
        for guild_id in guild_ids:
            await redis_queue.set_execution_owner(
                guild_id, self.execution_mode_for(guild_id)
            )

    async def enqueue_notification(
        self,
        guild_id: str,
        notification: Notification,
        *,
        passive: bool = False,
        watcher_usage: dict[str, dict] | None = None,
    ) -> None:
        """Route one notification to the embedded and/or extracted consumer.

        ``shadow`` deliberately publishes both copies while only the embedded
        consumer owns side effects. ``external`` publishes only Redis and never
        starts an in-process guild consumer.
        """
        destination = self.execution_mode_for(guild_id)
        redis_queue = self.redis_notification_queue()
        if redis_queue is not None:
            await redis_queue.set_execution_owner(guild_id, destination)
        if destination != EXTERNAL_EXECUTION_MODE:
            self.guild_state_for(int(guild_id)).queue.push(notification)
        if destination == EMBEDDED_EXECUTION_MODE:
            return

        if redis_queue is None:
            message = "proactive external notification queue is unavailable"
            if destination == EXTERNAL_EXECUTION_MODE:
                raise RuntimeError(message)
            logger.warning(message)
            return
        envelope = NotificationEnvelope.from_notification(
            notification,
            guild_id=guild_id,
            passive=passive,
            watcher_usage=watcher_usage,
        )
        if destination == SHADOW_EXECUTION_MODE:
            await redis_queue.publish_shadow(envelope)
            return
        await redis_queue.publish(envelope)

    def state_for(self, guild_id: int, channel_id: int) -> ChannelProducerState:
        state = self.channel_states.get(channel_id)
        if state is None:
            state = ChannelProducerState(
                guild_id=str(guild_id), channel_id=str(channel_id)
            )
            self.channel_states[channel_id] = state
        return state

    def guild_state_for(self, guild_id: int) -> GuildAgentState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildAgentState(guild_id=str(guild_id))
            self.guild_states[guild_id] = state
            if (
                self.start_consumers
                and self.execution_mode_for(str(guild_id)) != EXTERNAL_EXECUTION_MODE
            ):
                state.consumer_task = asyncio.create_task(_consumer_loop(state))
        return state

    def agent_runner_for(self, state: GuildAgentState) -> KimiAgentRunner:
        if state.agent_runner is None:
            guild = self.bot.cache.get_guild(int(state.guild_id))
            me = self.bot.get_me()

            async def compaction_summarize(messages) -> str:
                # The agent writes its own carry-forward memory: the folded
                # transcript rides as message history and the agent's own
                # model decides what its future self needs to keep. Every
                # attempt is bounded — an unbounded ~100k-token summarize
                # once stalled the whole consumer loop (2026-09-01) — and
                # failure degrades to a watcher-model skim, then truncation,
                # so a wake can never block on summarization.
                try:
                    summary, usage = await asyncio.wait_for(
                        self_compaction_summary(
                            build_twopass_model(self.agent_model_id), messages
                        ),
                        timeout=COMPACTION_TIMEOUT_SECONDS,
                    )
                    logger.info("proactive self-compaction: %s", usage)
                    return summary
                except Exception:  # noqa: BLE001 — fall back, never hang a wake
                    logger.exception(
                        "self-compaction failed; falling back to a watcher-model skim"
                    )
                try:
                    dumped = ModelMessagesTypeAdapter.dump_json(messages).decode()
                    summary, usage = await asyncio.wait_for(
                        self.skim().skim(dumped),
                        timeout=COMPACTION_TIMEOUT_SECONDS,
                    )
                    logger.info(
                        "proactive history compaction (skim fallback): %s", usage
                    )
                    return summary
                except Exception:  # noqa: BLE001 — truncation beats a hung agent
                    logger.exception(
                        "compaction summarize failed on both models; "
                        "compacting by truncation"
                    )
                    return (
                        "[Earlier history could not be summarized this wake "
                        "and was dropped; only recent messages follow.]"
                    )

            state.agent_runner = KimiAgentRunner(
                agent=build_proactive_agent(
                    build_twopass_model(self.agent_model_id),
                    system_prompt=build_guild_agent_system_prompt(
                        bot_display_name=me.username if me else "smarter-bot",
                        bot_user_id=str(me.id) if me else "",
                        guild_name=(getattr(guild, "name", None) or state.guild_id),
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
    async for message in bot.rest.fetch_messages(channel_id).limit(HISTORY_FETCH_LIMIT):
        converted = channel_message_from_hikari(message)
        if converted.id not in exclude_ids:
            fetched.append(converted)
    fetched.reverse()
    return fetched


def _message_is_after_cursor(message: ChannelMessage, cursor: str | None) -> bool:
    if cursor is None:
        return True
    if message.id.isdigit() and cursor.isdigit():
        return int(message.id) > int(cursor)
    return message.id != cursor


async def _record_usage(
    service,
    *,
    guild_id: str,
    channel_id: str,
    metered_at: datetime,
    passive: bool,
    responses: int,
    operation: str,
    usage_by_model: dict[str, dict] | None,
) -> None:
    entries = [
        {
            "model_id": model_id,
            "operation": operation,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
        }
        for model_id, usage in (usage_by_model or {}).items()
    ]
    if not entries:
        return
    try:
        await service.record_wake_usage(
            guild_id,
            channel_id,
            wake_id=uuid4().hex,
            metered_at=metered_at,
            passive=passive,
            responses=responses,
            entries=entries,
        )
    except Exception:  # noqa: BLE001 — the ledger must not stop either loop
        logger.exception("failed to persist proactive usage")


async def _run_producer(state: ChannelProducerState, *, passive: bool = False) -> None:
    """Review the next buffered batch and enqueue any watcher wake."""
    run = _runtime()
    service = run.settings_service()
    if service is None:
        return
    async with state.lock:
        buffered = state.buffer
        state.buffer = []
        buffered_ids = {message.id for message in buffered}
        live_notified_directed_ids = state.pending_directed_ids & buffered_ids
        state.pending_directed_ids.difference_update(buffered_ids)
    new_messages = [
        message
        for message in buffered
        if _message_is_after_cursor(message, state.last_reviewed_message_id)
    ]
    if not new_messages and not passive:
        return
    settings = await service.get_settings(state.guild_id, state.channel_id)
    if not settings.enabled:
        return

    activated_at = datetime.now(UTC)
    me = run.bot.get_me()
    bot_user_id = str(me.id) if me else ""
    history = await _fetch_history(
        run.bot,
        int(state.channel_id),
        exclude_ids={message.id for message in new_messages},
    )
    visible = ChannelEnvironment(
        visible=[*history, *new_messages], bot_user_id=bot_user_id
    )
    directed_ids = set(bot_directed_message_ids(new_messages, visible, bot_user_id))
    already_notified_directed_ids = directed_ids & live_notified_directed_ids
    watcher_messages = [
        message
        for message in new_messages
        if message.id not in already_notified_directed_ids
    ]
    instruction_store = InstructionStore.from_stored(
        OPERATING_POLICY_BRIEF, settings.watch_addendum
    )
    produced_queue = NotificationQueue()
    usage_by_model: dict[str, dict] = {}
    details: dict = {}
    if watcher_messages or passive:
        producer = WatcherProducer(
            watcher=run.watcher(),
            instruction_store=instruction_store,
            watcher_model_id=run.watcher_model_id,
            notification_queue=produced_queue,
            bot_display_name=me.username if me else "the bot",
        )
        context = ActivationContext(
            channel_name=_channel_name(run.bot, state),
            guild_name=str(state.guild_id),
            bot_user_id=bot_user_id,
            activated_at=activated_at,
            history=history,
            new_messages=watcher_messages,
            channel_id=state.channel_id,
        )
        usage_by_model = await producer.produce(context)
        details = producer.details
        if (
            producer.wake_produced
            and run.execution_mode_for(state.guild_id) != EXTERNAL_EXECUTION_MODE
        ):
            run.guild_state_for(int(state.guild_id)).pending_passive_wake = passive
        for notification in produced_queue.items:
            await run.enqueue_notification(
                state.guild_id,
                notification,
                passive=passive,
                watcher_usage=usage_by_model,
            )
        await _record_usage(
            service,
            guild_id=state.guild_id,
            channel_id=state.channel_id,
            metered_at=activated_at,
            passive=passive,
            responses=0,
            operation="watcher",
            usage_by_model=usage_by_model,
        )
    if new_messages:
        state.last_reviewed_message_id = new_messages[-1].id
    state.last_wake_at = time.monotonic()
    logger.info(
        "proactive producer channel=%s reviewed=%d passive=%s wake=%s",
        state.channel_id,
        len(new_messages),
        passive,
        details.get("watcher", {}).get("wake"),
    )


async def _consume_guild_once(state: GuildAgentState) -> None:
    """Drain one guild wake and route every action to its named channel."""
    run = _runtime()
    service = run.settings_service()
    if service is None:
        state.queue.drain()
        return
    try:
        enabled_rows = await service.list_enabled_channels(state.guild_id)
    except APIError:
        logger.warning(
            "proactive enabled-channel lookup failed guild=%s",
            state.guild_id,
            exc_info=True,
        )
        await asyncio.sleep(SETTINGS_RETRY_BACKOFF_SECONDS)
        return
    if not enabled_rows:
        state.queue.drain()
        return

    me = run.bot.get_me()
    bot_user_id = str(me.id) if me else ""
    activated_at = datetime.now(UTC)
    enabled_channels = {}
    instruction_stores = {}
    persisted_updates = {}
    for row in enabled_rows:
        channel_id = str(row.channel_id)
        channel_name = _channel_name_for_id(run.bot, channel_id)
        enabled_channels[channel_id] = channel_name
        instruction_store = InstructionStore.from_stored(
            OPERATING_POLICY_BRIEF, row.watch_addendum
        )
        persisted_updates[channel_id] = instruction_store.updates
        for expired in instruction_store.prune_expired(now=activated_at):
            state.queue.push(
                instruction_expired_notification(
                    instruction_id=expired.instruction_id,
                    text=expired.text,
                    created_at=activated_at,
                    channel_id=channel_id,
                    channel_name=channel_name,
                )
            )
        instruction_stores[channel_id] = instruction_store

    runner = run.agent_runner_for(state)
    history_store = run.history_store()
    if history_store is not None and not state.history_loaded:
        try:
            runner.history = await history_store.read_guild(int(state.guild_id))
        except Exception:  # noqa: BLE001 — stored history is a cache
            logger.exception("failed to load proactive guild history")
        state.history_loaded = True

    brief_preamble = ""
    now = time.monotonic()
    if now - state.memory_refreshed_at >= MEMORY_REFRESH_SECONDS:
        brief_preamble = await load_memory_block(run, state.guild_id)
        state.memory_refreshed_at = now

    async def channel_envs(channel_id: str) -> ChannelEnvironment:
        try:
            history = await _fetch_history(run.bot, int(channel_id), exclude_ids=set())
        except hikari.HikariError:
            # An unreachable channel must not discard every channel's wake.
            logger.warning(
                "proactive channel history unavailable channel=%s",
                channel_id,
                exc_info=True,
            )
            history = []
        return ChannelEnvironment(visible=history, bot_user_id=bot_user_id)

    wake_deps: list[ProactiveDeps] = []

    def request_mode(channel_id: str, mode: str, minutes: int) -> str:
        channel_state = run.state_for(int(state.guild_id), int(channel_id))
        if mode == "active":
            duration = max(1, minutes)
            channel_state.active_until = time.monotonic() + duration * 60
            until = datetime.now(UTC) + timedelta(minutes=duration)
            confirmation = f"Monitoring mode set to active for {minutes} minutes."
        else:
            channel_state.active_until = 0.0
            until = None
            confirmation = "Monitoring mode set to passive."
        state.queue.push(
            mode_change_notification(
                mode=mode,
                cause="you requested it",
                until=until,
                created_at=datetime.now(UTC),
                channel_id=channel_id,
                channel_name=enabled_channels[channel_id],
            )
        )
        return confirmation

    def drain_notifications() -> str:
        items, dropped = state.queue.drain()
        if not items:
            return "No new notifications."
        return render_notifications(items, dropped)

    def deps_factory(**kwargs):
        deps = ProactiveDeps(
            bot=run.bot,
            channel_id=0,
            guild_id=int(state.guild_id),
            channel_name=None,
            request_mode=request_mode,
            drain_notifications=drain_notifications,
            **kwargs,
        )
        wake_deps.append(deps)
        return deps

    consumer = AgentConsumer(
        agent_runner=runner,
        skim=run.skim(),
        agent_model_id=run.agent_model_id,
        notification_queue=state.queue,
        watcher_model_id=run.watcher_model_id,
        deps_factory=deps_factory,
        brief_preamble=brief_preamble,
        instruction_stores=instruction_stores,
        enabled_channels=enabled_channels,
        channel_envs=channel_envs,
    )
    guild = run.bot.cache.get_guild(int(state.guild_id))
    context = ActivationContext(
        channel_name="",
        guild_name=getattr(guild, "name", None) or state.guild_id,
        bot_user_id=bot_user_id,
        activated_at=activated_at,
        history=[],
        new_messages=[],
        channel_id="",
    )
    passive = state.pending_passive_wake
    state.pending_passive_wake = False
    result = await consumer.consume(context)

    dispatched_responses = 0
    sent_message_parts = 0
    for response in result.responses:
        if response.channel_id not in enabled_channels:
            logger.warning(
                "proactive response rejected for disabled channel=%s",
                response.channel_id,
            )
            continue
        response_message_parts = await dispatch_response(
            run.bot,
            channel_id=int(response.channel_id),
            content=response.content,
            reply_to_id=response.reply_to_id,
        )
        sent_message_parts += response_message_parts
        if response_message_parts:
            dispatched_responses += 1
    for reaction in result.reactions:
        if (
            reaction.channel_id not in enabled_channels
            or not reaction.message_id.isdigit()
        ):
            continue
        try:
            await run.bot.rest.add_reaction(
                int(reaction.channel_id),
                int(reaction.message_id),
                reaction.emoji,
            )
        except Exception:  # noqa: BLE001
            logger.exception("proactive reaction failed")

    images_by_channel: dict[str, list] = {}
    for deps in wake_deps:
        for pending_image in deps.pending_images:
            if pending_image.channel_id in enabled_channels:
                images_by_channel.setdefault(pending_image.channel_id, []).append(
                    pending_image
                )
    for channel_id, pending_images in images_by_channel.items():
        reply_target = next(
            (
                int(response.reply_to_id)
                for response in result.responses
                if response.channel_id == channel_id
                and response.reply_to_id
                and response.reply_to_id.isdigit()
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
            await run.bot.rest.create_message(int(channel_id), **image_kwargs)
        except Exception:  # noqa: BLE001 — images are best-effort extras
            logger.exception("proactive image post failed")

    if history_store is not None:
        try:
            await history_store.write_guild(int(state.guild_id), runner.history)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.exception("failed to persist proactive guild history")
        for producer_state in run.channel_states.values():
            if (
                producer_state.guild_id != state.guild_id
                or producer_state.last_reviewed_message_id is None
                or not producer_state.last_reviewed_message_id.isdigit()
            ):
                continue
            try:
                await history_store.write_cursor(
                    int(producer_state.channel_id),
                    guild_id=producer_state.guild_id,
                    last_message_id=producer_state.last_reviewed_message_id,
                )
            except Exception:  # noqa: BLE001 — persistence is best-effort
                logger.exception("failed to persist proactive cursor")
    for channel_id, instruction_store in instruction_stores.items():
        if instruction_store.updates == persisted_updates[channel_id]:
            continue
        try:
            await service.set_watch_addendum(
                state.guild_id, channel_id, instruction_store.to_stored()
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.exception("failed to persist watch instructions")
    await _record_usage(
        service,
        guild_id=state.guild_id,
        channel_id="guild-wide",
        metered_at=activated_at,
        passive=passive,
        responses=dispatched_responses,
        operation="agent",
        usage_by_model=result.usage_by_model,
    )
    logger.info(
        "proactive guild agent guild=%s responses=%d message_parts=%d reactions=%d "
        "tokens_in=%d tokens_out=%d",
        state.guild_id,
        dispatched_responses,
        sent_message_parts,
        len(result.reactions),
        result.input_tokens,
        result.output_tokens,
    )


async def _consumer_loop_iteration(state: GuildAgentState) -> None:
    await state.queue.wait_for_wake()
    await _consume_guild_once(state)


async def _consumer_loop(state: GuildAgentState) -> None:
    while True:
        try:
            await _consumer_loop_iteration(state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed wake must not kill the loop
            logger.exception("proactive agent wake failed guild=%s", state.guild_id)


def _engagement_notification(
    bot,
    state: ChannelProducerState,
    message,
    converted: ChannelMessage,
    bot_user_id: str,
) -> Notification:
    """Build a mention/reply notification for the guild agent, verbatim."""
    channel_name = _channel_name(bot, state)
    mention_ids = getattr(message, "user_mentions_ids", None) or ()
    if bot_user_id in {str(mention_id) for mention_id in mention_ids}:
        return mention_notification(
            converted,
            channel_id=state.channel_id,
            channel_name=channel_name,
        )
    referenced = getattr(message, "referenced_message", None)
    replied_to = (
        channel_message_from_hikari(referenced) if referenced is not None else None
    )
    return reply_notification(
        converted,
        replied_to,
        channel_id=state.channel_id,
        channel_name=channel_name,
    )


def _schedule_producer(state: ChannelProducerState) -> None:
    if state.timer is not None and not state.timer.done():
        state.timer.cancel()

    async def fire_after_delay() -> None:
        delay = compute_fire_delay(state.first_at, state.last_at, time.monotonic())
        await asyncio.sleep(delay)
        task = asyncio.create_task(_run_producer_guarded(state))
        state.producer_tasks.add(task)
        task.add_done_callback(state.producer_tasks.discard)

    state.timer = asyncio.create_task(fire_after_delay())


async def _run_producer_guarded(state: ChannelProducerState) -> None:
    try:
        await _run_producer(state)
    except Exception:  # noqa: BLE001 — a failed run must not kill scheduling
        logger.exception("proactive producer failed channel=%s", state.channel_id)


async def _persist_active_window(
    run: ProactiveRuntime, state: ChannelProducerState
) -> None:
    """Store the wall-clock window deadline so restarts keep conversations
    responsive; a Redis blip only costs the persistence, never the wake."""
    store = run.history_store()
    if store is None:
        return
    try:
        await store.write_active_until(
            int(state.channel_id),
            until_epoch=time.time() + ACTIVE_WINDOW_SECONDS,
            ttl_seconds=ACTIVE_WINDOW_SECONDS,
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.exception("failed to persist active window channel=%s", state.channel_id)


async def _restore_active_window(
    run: ProactiveRuntime, state: ChannelProducerState
) -> None:
    """Rehydrate a live active window once per channel after a restart."""
    if state.active_window_restored:
        return
    state.active_window_restored = True
    store = run.history_store()
    if store is None:
        return
    try:
        stored_until = await store.read_active_until(int(state.channel_id))
    except Exception:  # noqa: BLE001 — restoration is best-effort
        logger.exception("failed to read active window channel=%s", state.channel_id)
        return
    remaining = (stored_until or 0) - time.time()
    if remaining > 0:
        state.active_until = max(state.active_until, time.monotonic() + remaining)


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
    await _restore_active_window(run, state)
    now = time.monotonic()
    if not state.buffer:
        state.first_at = now
    state.last_at = now
    converted = channel_message_from_hikari(event.message)
    state.buffer.append(converted)

    me = run.bot.get_me()
    bot_user_id = str(me.id) if me else ""
    engaged = event_engages_bot(event.message, bot_user_id)
    if engaged:
        # A member engaged the bot: active ingest for a while, so the
        # conversation feels responsive.
        if now >= state.active_until:
            await run.enqueue_notification(
                str(event.guild_id),
                mode_change_notification(
                    mode="active",
                    cause=f"{event.author.username} engaged the bot",
                    until=datetime.now(UTC) + timedelta(seconds=ACTIVE_WINDOW_SECONDS),
                    created_at=datetime.now(UTC),
                    channel_id=state.channel_id,
                    channel_name=_channel_name(run.bot, state),
                ),
            )
        state.active_until = now + ACTIVE_WINDOW_SECONDS
        await _persist_active_window(run, state)
        await run.enqueue_notification(
            str(event.guild_id),
            _engagement_notification(
                run.bot,
                state,
                event.message,
                converted,
                bot_user_id,
            ),
        )
        state.pending_directed_ids.add(converted.id)
    if now < state.active_until:
        _schedule_producer(state)
    # Passive channels leave the buffer for the 15-minute sweep.


DISCORD_EPOCH_MS = 1420070400000


def _synthetic_message_id() -> str:
    """A now-timestamped snowflake so synthetic entries pass the review
    cursor exactly once, like a real message would."""
    return str((int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22)


def _reaction_channel_message(
    *, reactor_name: str, reactor_id: str, emoji: str, message_id: str
) -> ChannelMessage:
    """A reaction as a buffer entry, so the watcher reviews it at the same
    cadence as plain messages (sweep in passive, debounce in active)."""
    return ChannelMessage(
        id=_synthetic_message_id(),
        timestamp=datetime.now(UTC),
        author_id=reactor_id,
        author_name=reactor_name,
        author_display=reactor_name,
        is_bot=False,
        content=f"[reacted {emoji} to the bot's message {message_id}]",
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


@plugin.listener(hikari.GuildReactionAddEvent)
async def on_guild_reaction(event: hikari.GuildReactionAddEvent) -> None:
    """Reactions to the bot's messages flow like plain messages: they join
    the channel's producer buffer for watcher review at normal cadence, and
    a low-signal notification carries the detail into the next wake."""
    run = runtime
    if run is None or not event.guild_id:
        return
    me = run.bot.get_me()
    if me is None or str(event.user_id) == str(me.id):
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
    message = run.bot.cache.get_message(event.message_id)
    if message is None:
        try:
            message = await run.bot.rest.fetch_message(
                event.channel_id, event.message_id
            )
        except Exception:  # noqa: BLE001 — a lost lookup only drops one signal
            logger.warning("proactive reaction message fetch failed", exc_info=True)
            return
    author = getattr(message, "author", None)
    if author is None or str(author.id) != str(me.id):
        return
    reactor = getattr(event, "member", None)
    reactor_name = (
        getattr(reactor, "display_name", None)
        or getattr(reactor, "username", None)
        or str(event.user_id)
    )
    emoji = getattr(event, "emoji_name", None) or "a custom emoji"
    await run.enqueue_notification(
        str(event.guild_id),
        reaction_notification(
            reactor_name=reactor_name,
            reactor_id=str(event.user_id),
            emoji=emoji,
            message_id=str(event.message_id),
            message_preview=getattr(message, "content", "") or "",
            created_at=datetime.now(UTC),
            channel_id=str(event.channel_id),
            channel_name=_channel_name_for_id(run.bot, str(event.channel_id)),
        ),
    )
    # Same review cadence as a plain message: buffer for the watcher, and
    # arm the fast debounce only if the channel is already in its active
    # window (a reaction is low signal — it never opens one).
    state = run.state_for(event.guild_id, event.channel_id)
    await _restore_active_window(run, state)
    now = time.monotonic()
    if not state.buffer:
        state.first_at = now
    state.last_at = now
    state.buffer.append(
        _reaction_channel_message(
            reactor_name=reactor_name,
            reactor_id=str(event.user_id),
            emoji=emoji,
            message_id=str(event.message_id),
        )
    )
    if now < state.active_until:
        _schedule_producer(state)


async def _passive_sweep(run: ProactiveRuntime) -> None:
    """One 15-minute pass: review passive buffers and long-idle channels."""
    for state in list(run.channel_states.values()):
        try:
            if state.buffer:
                await _run_producer(state, passive=True)
                continue
            idle_for = time.monotonic() - max(state.last_wake_at, state.last_at)
            if idle_for >= PASSIVE_SECONDS:
                await _run_producer(state, passive=True)
        except Exception:  # noqa: BLE001 — one channel must not kill the ticker
            logger.exception("passive sweep failed channel=%s", state.channel_id)


async def _passive_ticker() -> None:
    delay = FIRST_PASSIVE_SWEEP_SECONDS
    while True:
        await asyncio.sleep(delay)
        run = runtime
        if run is None:
            return
        await _passive_sweep(run)
        delay = PASSIVE_SECONDS


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


async def _recovery_channel_settings(
    run: ProactiveRuntime, guild_id: str, channel_id: int
):
    """Wait out a transient settings outage without abandoning the cursor."""
    while True:
        service = run.settings_service()
        if service is not None:
            try:
                return await service.get_settings(guild_id, str(channel_id))
            except APIError:
                logger.warning(
                    "proactive recovery settings unavailable channel=%s",
                    channel_id,
                    exc_info=True,
                )
        await asyncio.sleep(SETTINGS_RETRY_BACKOFF_SECONDS)


async def _recover_channels(run: ProactiveRuntime) -> None:
    """Replay what enabled channels received while the bot was down."""
    store = run.history_store()
    if store is None:
        return
    for channel_id in await store.cursor_channel_ids():
        try:
            cursor = await store.read_cursor(channel_id)
            if not cursor:
                continue
            settings = await _recovery_channel_settings(
                run, cursor["guild_id"], channel_id
            )
            if not settings.enabled:
                continue
            missed = await _fetch_missed(run.bot, channel_id, cursor["last_message_id"])
            if not missed:
                continue
            state = run.state_for(int(cursor["guild_id"]), channel_id)
            state.buffer.extend(missed)
            state.first_at = state.last_at = time.monotonic()
            await run.enqueue_notification(
                cursor["guild_id"],
                recovery_notification(
                    missed_count=len(missed),
                    created_at=datetime.now(UTC),
                    channel_id=state.channel_id,
                    channel_name=_channel_name(run.bot, state),
                ),
            )
            logger.info(
                "proactive recovery: %d missed messages in channel %s",
                len(missed),
                channel_id,
            )
            await _run_producer(state, passive=True)
        except Exception:  # noqa: BLE001 — one channel must not kill recovery
            logger.exception("proactive recovery failed channel=%s", channel_id)


def _redis_text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _apply_control_command(
    run: ProactiveRuntime, command: ControlCommand
) -> None:
    """Apply one worker request to the bot-owned watcher schedule."""
    state = run.state_for(int(command.guild_id), int(command.channel_id))
    now = datetime.now(UTC)
    if command.mode == "active":
        duration = max(1, command.minutes)
        state.active_until = time.monotonic() + duration * 60
        until = now + timedelta(minutes=duration)
        store = run.history_store()
        if store is not None:
            await store.write_active_until(
                int(command.channel_id),
                until_epoch=until.timestamp(),
                ttl_seconds=duration * 60,
            )
    else:
        state.active_until = 0.0
        until = None
        store = run.history_store()
        if store is not None:
            await store.write_active_until(
                int(command.channel_id), until_epoch=0, ttl_seconds=1
            )
    await run.enqueue_notification(
        command.guild_id,
        mode_change_notification(
            mode=command.mode,
            cause="you requested it",
            until=until,
            created_at=now,
            channel_id=command.channel_id,
            channel_name=_channel_name_for_id(run.bot, command.channel_id),
        ),
    )


async def _control_loop(run: ProactiveRuntime) -> None:
    """Consume idempotent watcher-control commands emitted by workers."""
    redis_client = run.bot.d.get("chat_memory_redis")
    if redis_client is None:
        return
    try:
        await redis_client.xgroup_create(
            CONTROL_STREAM_KEY, CONTROL_GROUP, id="0", mkstream=True
        )
    except Exception as error:  # redis-py has sync/async ResponseError variants
        if "BUSYGROUP" not in str(error):
            raise
    consumer = f"{socket.gethostname()}-{id(run)}"
    while True:
        reclaimed = await redis_client.xautoclaim(
            CONTROL_STREAM_KEY,
            CONTROL_GROUP,
            consumer,
            60_000,
            "0-0",
            count=20,
        )
        records = (
            [(CONTROL_STREAM_KEY, reclaimed[1])]
            if reclaimed and reclaimed[1]
            else await redis_client.xreadgroup(
                CONTROL_GROUP,
                consumer,
                {CONTROL_STREAM_KEY: ">"},
                count=20,
                block=30_000,
            )
        )
        for _stream, entries in records or ():
            for stream_id, fields in entries:
                try:
                    payload = fields.get(b"payload", fields.get("payload"))
                    command = ControlCommand.model_validate_json(_redis_text(payload))
                    processed_key = f"{CONTROL_PROCESSED_PREFIX}:{command.command_id}"
                    if not await redis_client.exists(processed_key):
                        await _apply_control_command(run, command)
                        await redis_client.set(processed_key, "1", ex=7 * 24 * 60 * 60)
                    await redis_client.xack(
                        CONTROL_STREAM_KEY, CONTROL_GROUP, stream_id
                    )
                    await redis_client.xdel(CONTROL_STREAM_KEY, stream_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "failed proactive control command stream_id=%s",
                        _redis_text(stream_id),
                    )


@plugin.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    run = runtime
    if run is not None and run.passive_task is None:
        await run.sync_execution_ownership()
        run.passive_task = asyncio.create_task(_passive_ticker())
        run.recovery_task = asyncio.create_task(_recover_channels(run))
        run.control_task = asyncio.create_task(_control_loop(run))


@plugin.command
@lightbulb.command("proactive", "Proactive chat bot for this channel (moderators only)")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def proactive_group(ctx: lightbulb.Context) -> None:
    pass


async def _set_enabled(ctx: lightbulb.Context, enabled: bool) -> None:
    if await deny_without_moderator_permissions(ctx, MODERATOR_DENIAL_MESSAGE):
        return
    service = _runtime().settings_service()
    if service is None:
        await ctx.respond(
            "Proactive settings service is unavailable.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return
    was_enabled = (
        await service.get_settings(str(ctx.guild_id), str(ctx.channel_id))
    ).enabled
    await service.set_enabled(str(ctx.guild_id), str(ctx.channel_id), enabled)
    if enabled and not was_enabled:
        # Wake the guild agent to get oriented in its new channel.
        run = _runtime()
        await run.enqueue_notification(
            str(ctx.guild_id),
            channel_enabled_notification(
                created_at=datetime.now(UTC),
                channel_id=str(ctx.channel_id),
                channel_name=_channel_name_for_id(run.bot, str(ctx.channel_id)),
            ),
        )
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
    if await deny_without_moderator_permissions(ctx, MODERATOR_DENIAL_MESSAGE):
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
    state = _runtime().channel_states.get(ctx.channel_id)
    mode = (
        "active"
        if settings.enabled
        and state is not None
        and time.monotonic() < state.active_until
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
    if runtime is not None:
        if runtime.passive_task is not None:
            runtime.passive_task.cancel()
        if runtime.recovery_task is not None:
            runtime.recovery_task.cancel()
        if runtime.control_task is not None:
            runtime.control_task.cancel()
        for state in runtime.channel_states.values():
            if state.timer is not None:
                state.timer.cancel()
            for producer_task in state.producer_tasks:
                producer_task.cancel()
        for state in runtime.guild_states.values():
            if state.consumer_task is not None:
                state.consumer_task.cancel()
    runtime = None
    bot.remove_plugin(plugin)
