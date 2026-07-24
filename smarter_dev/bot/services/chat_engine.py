"""Per-channel engine that drives the chat agent.

Lifecycle:
- A channel engine is created when the bot is engaged (@mention or reply to a
  bot message). The engine fires the agent immediately with the most recent
  channel context.
- While active, every non-bot message in the channel is fed to ``observe``.
  The engine batches them in a queue and re-fires the agent when either:
    * the queue has had 5 seconds of idle since the last message, OR
    * the queue has reached 15 messages.
- Activations are serialised: the engine never runs the agent twice
  concurrently. If new messages arrive *during* an agent run, they keep
  queueing; immediately after the run completes the engine re-checks the
  fire conditions and runs again if met.
- The engine deactivates (and the watcher is removed from the global registry)
  when any of:
    * the agent returns ``continue_watching=False``,
    * 3 consecutive ``NoResponse`` turns occur, or
    * 30 minutes pass since the last message the agent sent.

A regex stop-detector runs on every observed message — if the user tells the
bot to stop, the engine deactivates silently and the channel goes on cooldown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import hikari
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.usage import RunUsage
from redis.exceptions import RedisError

from smarter_dev.bot.agents.chat_agent import DEFAULT_MODEL as CHAT_DEFAULT_MODEL
from smarter_dev.bot.agents.chat_agent import MODEL_ENV_VAR as CHAT_MODEL_ENV_VAR
from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_agent import resolved_reasoning_level
from smarter_dev.bot.agents.chat_compaction import drain_collection
from smarter_dev.bot.agents.chat_compaction import set_last_model_call
from smarter_dev.bot.agents.chat_compaction import start_collection
from smarter_dev.bot.agents.chat_context import build_followup_input
from smarter_dev.bot.agents.chat_context import build_initial_input
from smarter_dev.bot.agents.chat_input_format import build_agent_call
from smarter_dev.bot.agents.message_gate import GateMessage
from smarter_dev.bot.agents.message_gate import filter_messages
from smarter_dev.bot.agents.chat_models import ResponseBody
from smarter_dev.bot.agents.chat_models import TurnDecision
from smarter_dev.bot.agents.response_fitting import SUMMARIZE_THRESHOLD
from smarter_dev.bot.agents.response_fitting import fit_overlong_response
from smarter_dev.bot.agents.response_fitting import split_for_discord
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import GeneratedImage
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.bot.services.channel_token_budget import add_fallback_usage
from smarter_dev.bot.services.channel_token_budget import add_usage
from smarter_dev.bot.services.default_model_override import read_default_model_override
from smarter_dev.bot.services.channel_token_budget import fallback_ended_key
from smarter_dev.bot.services.channel_token_budget import fallback_flag_key
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.channel_token_budget import over_budget_reset_epoch
from smarter_dev.bot.services.chat_conversation_persistence import end_engagement
from smarter_dev.bot.services.chat_conversation_persistence import persist_error
from smarter_dev.bot.services.chat_conversation_persistence import persist_turn
from smarter_dev.bot.services.chat_conversation_persistence import start_engagement
from smarter_dev.bot.services.chat_memory import get_chat_memory
from smarter_dev.bot.services.user_message_limit import DIRECTED_SCORE_THRESHOLD
from smarter_dev.bot.services.user_message_limit import format_usage_warning_notice
from smarter_dev.bot.services.user_message_limit import record_directed_messages
from smarter_dev.bot.utils.messages import fetch_channel_info
from smarter_dev.bot.utils.stop_detection import is_stop_request
from smarter_dev.bot.utils.stop_detection import set_channel_cooldown
from smarter_dev.bot.views.model_override_views import (
    MODEL_BUDGET_FALLBACK_CUSTOM_ID_PREFIX,
)

logger = logging.getLogger(__name__)

IDLE_FIRE_SECONDS = 5
QUEUE_FIRE_THRESHOLD = 15
MAX_NO_RESPONSE_TURNS = 3
INACTIVITY_TIMEOUT = timedelta(minutes=30)
MAX_RUNTIME = timedelta(hours=2)

# When a channel's model token budget is exhausted the engine stays quiet, but
# posts one short notice so the silence is explained. This throttle (a Redis
# SET NX EX) caps that notice to once per hour per channel so a busy channel
# can't be spammed while the budget stays spent.
BUDGET_NOTICE_COOLDOWN_SECONDS = 60 * 60
# How many channel messages immediately preceding the gated candidates are
# fetched as read-only context for the response-filter gate.
GATE_GROUNDING_LIMIT = 5
# ``{reset_tag}`` is a Discord relative timestamp (``<t:epoch:R>``) that the
# client renders as a live countdown ("in 25 minutes") to the window boundary.
_BUDGET_EXHAUSTED_NOTICE_TEMPLATE = (
    "This channel's model token budget is used up for now — I'll pick back "
    "up {reset_tag}."
)

# Appended to a text reply when a generated image can't be attached because the
# bot is missing the "Attach Files" permission — so the answer still lands and
# the gap is explained instead of silent.
_ATTACH_PERM_NOTE = (
    "\n\n-# (couldn't attach the generated image here — I'm missing the "
    '"Attach Files" permission in this channel)'
)


@dataclass
class _QueuedMessage:
    """A snapshot of a hikari message awaiting agent activation."""

    message: hikari.Message
    enqueued_at: datetime


@dataclass
class _VoiceOutcome:
    """Result of a voice send for one turn — surfaced to the persister."""

    sent_ok: bool
    tokens_input: int = 0
    tokens_output: int = 0
    model_name: str | None = None
    error: str | None = None


@dataclass
class _TurnDispatchOutcome:
    """Per-turn dispatch result returned by ``_apply_output`` so the engine
    can persist it before any subsequent deactivation tears state down."""

    voice: _VoiceOutcome | None = None
    deactivate_reason: str | None = None


@dataclass
class ChannelEngine:
    """Drives the chat agent for a single channel."""

    bot: Any  # hikari.GatewayBot; Any keeps tests light
    channel_id: int
    guild_id: int
    voice_send: Callable[[int, str, int | None, str | None], Awaitable[Any]]
    on_deactivate: Callable[[int], Awaitable[None]]

    queue: list[_QueuedMessage] = field(default_factory=list)
    queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fire_event: asyncio.Event = field(default_factory=asyncio.Event)
    activation_message: Any = None  # hikari.Message — set once via trigger_initial
    engagement_id: Any = None  # UUID — set after start_engagement persists

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    consecutive_no_response: int = 0
    active: bool = True
    _idle_task: asyncio.Task | None = None
    _runner_task: asyncio.Task | None = None
    _shutdown: bool = False
    # When this engagement last hit the model — feeds the compactor's
    # cache-warm/cold judgement. None on the first turn (treated as cold).
    _last_model_call_at: datetime | None = None

    # ------------------------------------------------------------------
    # Public API

    @property
    def is_expired(self) -> bool:
        """True once the engine has been idle past the inactivity window.

        The runner only evaluates the inactivity timeout lazily on its next
        fire, so an idle engine lingers in the registry with ``active=True``
        until something pokes it. The registry consults this so a mention that
        arrives after the window starts a *fresh* engagement rather than being
        consumed by the stale engine's deactivation (which previously required
        a second mention to wake the bot back up).
        """
        return datetime.now(UTC) - self.last_sent_at > INACTIVITY_TIMEOUT

    def start(self) -> None:
        """Begin the background runner. Call once after construction."""
        if self._runner_task is None:
            self._runner_task = asyncio.create_task(self._runner())

    async def expire(self) -> None:
        """Tear down a stale engine that a new engagement is superseding.

        Mirrors the runner's inactivity path (clears notes + history, ends the
        engagement on the dashboard) but is driven by the registry rather than
        a fire event.
        """
        await self._deactivate(send_notes_clear=True, reason="inactivity")

    def trigger_initial(self, trigger_message: hikari.Message) -> None:
        """Fire the agent immediately for the first turn of this engagement.

        ``trigger_message`` is the @mention or reply that woke the engine; it
        is passed through to the agent as the InitialAgentInput's
        ``activation_message`` so the agent can distinguish it from the
        pre-engagement ``channel_history`` context.
        """
        self.activation_message = trigger_message
        self.fire_event.set()

    def fire_now(self) -> None:
        """Force the runner to fire on the current queue immediately.

        Used by the mention plugin when an @mention or reply lands in a
        channel where the engine is already active — the message has been
        enqueued via ``observe`` and we want a response right now rather
        than waiting on the 5s idle timer.
        """
        self.fire_event.set()

    async def observe(self, event: hikari.MessageCreateEvent) -> None:
        """Feed an incoming non-bot message into the engine.

        Triggers a stop-heuristic check, enqueues the message, and schedules a
        fire either when the queue hits 15 entries or after 5 seconds of idle.
        """
        if not self.active:
            return

        content = event.content or ""
        if is_stop_request(content):
            await self._handle_stop()
            return

        async with self.queue_lock:
            self.queue.append(
                _QueuedMessage(
                    message=event.message,
                    enqueued_at=datetime.now(UTC),
                )
            )
            queue_full = len(self.queue) >= QUEUE_FIRE_THRESHOLD

        if queue_full:
            self.fire_event.set()
        else:
            self._schedule_idle_fire()

    async def shutdown(self) -> None:
        """Cancel background tasks and mark the engine inactive."""
        self._shutdown = True
        self.active = False
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self.fire_event.set()
        if self._runner_task and not self._runner_task.done():
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Internal

    def _schedule_idle_fire(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_fire())

    async def _idle_fire(self) -> None:
        try:
            await asyncio.sleep(IDLE_FIRE_SECONDS)
            self.fire_event.set()
        except asyncio.CancelledError:
            pass

    async def _handle_stop(self) -> None:
        logger.info(
            "Stop request detected in channel %s — deactivating engine", self.channel_id
        )
        set_channel_cooldown(self.channel_id)
        await self._deactivate(send_notes_clear=True, reason="stop_phrase")

    async def _deactivate(
        self,
        *,
        send_notes_clear: bool,
        reason: str = "shutdown",
    ) -> None:
        if not self.active:
            return
        self.active = False
        memory = get_chat_memory()
        if send_notes_clear:
            try:
                await memory.clear_notes(self.channel_id)
            except Exception:
                logger.exception(
                    "Failed to clear notes for channel %s", self.channel_id
                )
        try:
            await memory.clear_history(self.channel_id)
        except Exception:
            logger.exception(
                "Failed to clear chat history for channel %s", self.channel_id
            )
        # Best-effort: tell the dashboard the engagement is over.
        if self.engagement_id is not None:
            try:
                await end_engagement(
                    bot=self.bot,
                    engagement_id=self.engagement_id,
                    deactivation_reason=reason,
                )
            except Exception:
                logger.exception(
                    "Failed to finalise chat engagement for channel %s",
                    self.channel_id,
                )
        await self.on_deactivate(self.channel_id)
        self.fire_event.set()  # wake the runner so it can exit

    async def _runner(self) -> None:
        """Main loop — waits for fire events, runs the agent, repeats.

        Note: the runner doesn't fire on startup — it always waits for an
        explicit ``fire_event.set()`` (from ``trigger_initial`` or ``observe``)
        so the first activation is counted as a normal fire. The runner tracks
        whether it has fired yet so it can pass ``first_activation=True`` once.
        """
        first_activation = True
        try:
            while self.active and not self._shutdown:
                await self.fire_event.wait()
                self.fire_event.clear()
                if not self.active or self._shutdown:
                    break

                # Check inactivity timeout before running.
                if datetime.now(UTC) - self.last_sent_at > INACTIVITY_TIMEOUT:
                    logger.info(
                        "Engine for channel %s timed out (%s since last send) — deactivating",
                        self.channel_id,
                        INACTIVITY_TIMEOUT,
                    )
                    await self._deactivate(
                        send_notes_clear=True, reason="inactivity"
                    )
                    break

                if datetime.now(UTC) - self.started_at > MAX_RUNTIME:
                    logger.info(
                        "Engine for channel %s hit max runtime — deactivating",
                        self.channel_id,
                    )
                    await self._deactivate(
                        send_notes_clear=True, reason="max_runtime"
                    )
                    break

                activation_consumed = await self._run_once(
                    first_activation=first_activation
                )
                # Only retire the first-activation flag once a turn actually
                # ran. A turn skipped before the engagement was started (e.g.
                # over budget) leaves it set, so once the budget frees the next
                # fire still starts the engagement and persists its turns.
                if activation_consumed:
                    first_activation = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chat engine crashed for channel %s", self.channel_id)
            await self._deactivate(send_notes_clear=True, reason="crash")

    async def _run_once(self, *, first_activation: bool) -> bool:
        """Run one agent turn. Returns whether the first activation was consumed.

        Almost always True; False only when the turn was skipped *before* the
        engagement could start (over budget), so the runner keeps
        ``first_activation`` set and retries the initial turn on the next fire.
        """
        async with self.run_lock:
            turn_started_at = datetime.now(UTC)
            # Snapshot queue; new messages can keep arriving while we run.
            async with self.queue_lock:
                drained = [q.message for q in self.queue]
                self.queue.clear()

            memory = get_chat_memory()
            if first_activation:
                await memory.reset_idle_counter(self.channel_id)

            # Resolve any admin per-channel override up front: its response
            # filter (if set) decides which pending messages are worth a turn,
            # and its budget / fallback model steer the model resolution below.
            override = await self._get_channel_override()
            budget_redis = self._budget_redis()

            # Response-filter gate — runs BEFORE any model spend or budget check.
            # A restricted channel asks the cheap relevance gate which of the
            # candidate messages the admin's filter allows and drops the rest;
            # if nothing survives the whole turn is skipped (no model call, no
            # budget spend, no no-response strike, engagement untouched). Mentions
            # get no bypass — an off-topic @mention is dropped too.
            response_filter = (
                getattr(override, "response_filter", None)
                if override is not None
                else None
            )
            if response_filter and response_filter.strip():
                if first_activation:
                    # Judge the activation trigger together with anything queued
                    # since it: while the engine is already active every later
                    # message routes through ``observe`` (never a fresh
                    # ``trigger_initial``), so the queued messages are the only
                    # way a later on-topic message can start the engagement when
                    # the activating message itself was off-topic. The earliest
                    # survivor becomes the engagement's trigger.
                    candidates = [
                        message
                        for message in [self.activation_message, *drained]
                        if message is not None
                    ]
                    allowed = await self._gate_allows(response_filter, candidates)
                    survivors = [m for m in candidates if str(m.id) in allowed]
                    if not survivors:
                        logger.info(
                            "Response filter dropped the activation and %d "
                            "queued message(s) for channel %s — skipping turn "
                            "(engagement retained)",
                            len(drained),
                            self.channel_id,
                        )
                        # Retain first_activation (return False) so a later
                        # on-topic message still starts the engagement.
                        return False
                    self.activation_message = survivors[0]
                else:
                    allowed = await self._gate_allows(response_filter, drained)
                    survivors = [m for m in drained if str(m.id) in allowed]
                    if not survivors:
                        logger.info(
                            "Response filter dropped all %d queued message(s) "
                            "for channel %s — skipping turn",
                            len(drained),
                            self.channel_id,
                        )
                        return True
                    drained = survivors

            # Best reply-to anchor for any user-facing error message: the
            # activation trigger on first turn, otherwise the freshest
            # surviving queued message.
            error_reply_to: int | None = None
            if first_activation and self.activation_message is not None:
                error_reply_to = int(self.activation_message.id)
            elif drained:
                error_reply_to = int(drained[-1].id)

            try:
                if first_activation:
                    trigger = self.activation_message
                    if trigger is None:
                        logger.error(
                            "Initial activation for channel %s has no "
                            "trigger_message set — aborting fire",
                            self.channel_id,
                        )
                        return True
                    agent_input = await build_initial_input(
                        bot=self.bot,
                        channel_id=self.channel_id,
                        guild_id=self.guild_id,
                        memory=memory,
                        trigger_message=trigger,
                    )
                    history = []
                else:
                    if not drained:
                        # Engine fired with nothing new to react to. Skip.
                        return True
                    agent_input = await build_followup_input(
                        bot=self.bot,
                        channel_id=self.channel_id,
                        guild_id=self.guild_id,
                        queued=drained,
                        memory=memory,
                    )
                    history = await memory.read_history(self.channel_id)
            except Exception:
                logger.exception(
                    "Failed to build agent input for channel %s", self.channel_id
                )
                await self._post_error(
                    "Sorry, couldn't process that one — something went wrong "
                    "preparing your message.",
                    reply_to=error_reply_to,
                )
                return True

            new_count = (
                len(agent_input.channel_history) + 1
                if first_activation
                else len(agent_input.new_messages)
            )
            request_id = uuid.uuid4().hex[:8]
            logger.info(
                "[%s] Chat agent firing channel=%s first=%s new=%d history=%d",
                request_id,
                self.channel_id,
                first_activation,
                new_count,
                len(history),
            )

            # Enforce the override's token budget (and route around it via the
            # fallback model) before we spend a model call. Three regimes:
            #  * a free-fallback window is active — a member opted this channel
            #    into its fallback model while the primary's budget is spent, so
            #    budget enforcement is skipped and the fallback model runs;
            #  * otherwise an over-budget channel skips the turn (offering the
            #    fallback if one is configured);
            #  * a channel under budget (or with no override) runs its model.
            # A channel with no override always falls through unchanged (default
            # model, no budget checks); its usage is still metered after the run.
            fallback_active = (
                await self._fallback_window_active(budget_redis)
                if override is not None and budget_redis is not None
                else False
            )
            if fallback_active:
                # Free fallback model in effect — its spend does not count
                # against the cap, so no budget check. Reasoning stays unset so
                # the fallback model uses its own default.
                override_model_id = self._resolve_fallback_model_id(override)
                override_reasoning = None
            elif override is not None and budget_redis is not None:
                budget_reset_epoch = await over_budget_reset_epoch(
                    budget_redis,
                    str(self.channel_id),
                    override.daily_token_budget,
                    override.hourly_token_budget,
                )
                if budget_reset_epoch is not None:
                    logger.info(
                        "[%s] Channel %s over model token budget — skipping turn",
                        request_id,
                        self.channel_id,
                    )
                    await self._maybe_notice_budget_exhausted(
                        budget_redis,
                        reset_epoch=budget_reset_epoch,
                        reply_to=error_reply_to,
                        fallback_model_key=getattr(
                            override, "fallback_model_key", None
                        ),
                    )
                    # Skipped before start_engagement — report the first
                    # activation as NOT consumed so the engagement still starts
                    # (and turns persist) once the budget window resets.
                    return False
                # Under budget now. If a prior fallback window just ended,
                # announce the primary is back once before running its turn —
                # done only after the budget re-check so a channel that is still
                # over budget never sees a "primary is back" notice immediately
                # followed by a "budget exhausted" one (and the one-shot marker
                # survives so the genuine restoration is announced later).
                await self._maybe_notice_primary_restored(budget_redis, override)
                override_model_id = self._resolve_override_model_id(override)
                # Only carry the override's reasoning level when its model
                # actually applied — a stale model_key falls back to the default
                # model, which keeps its own reasoning config.
                override_reasoning = (
                    override.reasoning_level
                    if override_model_id is not None
                    else None
                )
            else:
                override_model_id = self._resolve_override_model_id(override)
                override_reasoning = (
                    override.reasoning_level
                    if override is not None and override_model_id is not None
                    else None
                )
            # A temporary bot-wide default-model override (set via the admin
            # ``/chat-default-model-override`` command, self-expiring in Redis)
            # substitutes for the configured default — so it applies only when
            # no channel override chose this turn's model.
            if override_model_id is None and budget_redis is not None:
                override_model_id, override_reasoning = (
                    await self._resolve_temporary_default(budget_redis)
                )
            # The model this turn actually runs on: the override's wire id when
            # one applied, otherwise the configured default. Persisted with the
            # turn so the dashboard prices tokens against the right model.
            resolved_model_name = override_model_id or os.environ.get(
                CHAT_MODEL_ENV_VAR, CHAT_DEFAULT_MODEL
            )
            # The reasoning level actually applied this turn: the override's
            # choice clamped onto what the resolved model supports (or the
            # model default when unset). Persisted with the turn so the
            # dashboard can attribute reasoning spend. Matches the model +
            # reasoning pair handed to ``get_chat_agent`` below.
            resolved_reasoning_wire = resolved_reasoning_level(
                override_model_id, override_reasoning
            )

            # Start engagement persistence on the very first turn — gives us
            # an engagement_id we attach to every persisted turn that follows.
            if first_activation and self.engagement_id is None:
                trigger = self.activation_message
                guild = self.bot.cache.get_guild(self.guild_id) if hasattr(
                    self.bot, "cache"
                ) else None
                guild_name = getattr(guild, "name", None) if guild else None
                channel_name = getattr(agent_input.channel, "name", None)
                self.engagement_id = await start_engagement(
                    bot=self.bot,
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    guild_name=guild_name,
                    channel_name=channel_name,
                    activation_user_id=int(trigger.author.id) if trigger else 0,
                    activation_username=(
                        trigger.author.username if trigger else "unknown"
                    ),
                    activation_message_id=int(trigger.id) if trigger else 0,
                )

            agent = get_chat_agent(override_model_id, override_reasoning)
            deps = ChatDeps(
                bot=self.bot,
                channel_id=self.channel_id,
                guild_id=self.guild_id,
                api_client=self._shared_api_client(),
            )
            # Install a per-run compaction collector. The history processor
            # appends events to it; we drain after the run.
            start_collection()
            set_last_model_call(self._last_model_call_at)
            image_quota = await self._fetch_image_quota()
            user_prompt, message_history = build_agent_call(
                agent_input,
                history,
                image_quota=image_quota,
                model_name=resolved_model_name,
                reasoning_level=resolved_reasoning_wire,
            )
            try:
                result = await agent.run(
                    user_prompt=user_prompt,
                    message_history=message_history,
                    deps=deps,
                )
            except Exception as error:
                # Even a failed run (probably) hit the model — later turns
                # inside the cache TTL should read warm.
                self._last_model_call_at = datetime.now(UTC)
                logger.exception(
                    "[%s] Chat agent run failed for channel %s",
                    request_id,
                    self.channel_id,
                )
                drain_collection()  # discard
                # The run may have crashed *after* generate_image already spent a
                # quota slot and stashed an image. Salvage it — the user paid for
                # it — while still reporting that the overall run failed.
                if deps.pending_images:
                    await self._post_images(
                        list(deps.pending_images), reply_to=error_reply_to
                    )
                admin_url = await persist_error(
                    bot=self.bot,
                    error=error,
                    engagement_id=self.engagement_id,
                    request_id=request_id,
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    model_name=(
                        error.model_name
                        if isinstance(error, ModelAPIError)
                        else resolved_model_name
                    ),
                    reasoning_level=resolved_reasoning_wire,
                    error_context={
                        "first_activation": first_activation,
                        "new_message_count": new_count,
                        "history_message_count": len(history),
                        "fallback_active": fallback_active,
                        "trigger_message_id": error_reply_to,
                    },
                )
                message = "Sorry, couldn't generate a reply for that one — try again?"
                if admin_url is not None:
                    message += (
                        f"\n-# Admin diagnostics: [view error details]({admin_url})"
                    )
                await self._post_error(
                    message,
                    reply_to=error_reply_to,
                )
                return True
            self._last_model_call_at = datetime.now(UTC)
            compaction_events = drain_collection()

            output = result.output
            tokens = _extract_tokens(result.usage())
            # A reply too long to send in two messages gets rewritten before
            # dispatch: the agent shortens its own draft (context-aware),
            # falling back to a Luna summary, then truncation. The
            # shorten re-run spends chat-model tokens, so they're folded into
            # this turn's metering and persisted totals.
            fit_extra_input = 0
            fit_extra_output = 0
            if (
                output.response is not None
                and output.response.message
                and len(output.response.message) > SUMMARIZE_THRESHOLD
            ):
                fit = await fit_overlong_response(
                    output.response.message,
                    agent=agent,
                    deps=deps,
                    message_history=list(result.all_messages()),
                )
                logger.info(
                    "[%s] Overlong reply (%d chars) fitted via %s to %d chars",
                    request_id,
                    len(output.response.message),
                    fit.method,
                    len(fit.text),
                )
                output = output.model_copy(
                    update={
                        "response": output.response.model_copy(
                            update={"message": fit.text}
                        )
                    }
                )
                fit_extra_input = fit.extra_input_tokens
                fit_extra_output = fit.extra_output_tokens
                tokens += fit_extra_input + fit_extra_output
            # Meter this turn's chat tokens against the channel's usage windows.
            # Every channel is metered (so ``/bot-usage`` always has numbers);
            # the budgets that *enforce* only exist on override channels.
            # Compaction runs on its own summarizer model and its tokens are not
            # in ``result.usage()``, so they are not counted here. A free-fallback
            # turn meters into display-only windows so its spend still shows in
            # ``/bot-usage`` but never counts toward the enforced budget.
            if budget_redis is not None:
                await self._record_budget_usage(
                    budget_redis, tokens, fallback_active=fallback_active
                )
            await self._charge_directed_messages(
                output, drained, first_activation=first_activation
            )
            logger.info(
                "[%s] Chat agent returned response=%s continue_watching=%s "
                "max_score=%s tokens=%s",
                request_id,
                "yes" if output.response is not None else "no",
                output.continue_watching,
                max((r.score for r in output.rankings), default=None),
                tokens,
            )

            # Persist the post-processor history for the next turn.
            try:
                await memory.write_history(
                    self.channel_id, list(result.all_messages())
                )
            except Exception:
                logger.exception(
                    "[%s] Failed to persist chat history for channel %s",
                    request_id,
                    self.channel_id,
                )

            dispatch = await self._apply_output(output, list(deps.pending_images))

            # Persist this turn to the operator dashboard (best-effort).
            try:
                triggering = (
                    [agent_input.activation_message.model_dump(mode="json")]
                    if first_activation
                    else [m.model_dump(mode="json") for m in agent_input.new_messages]
                )
                chat_usage = result.usage()
                chat_in = (
                    int(getattr(chat_usage, "input_tokens", 0) or 0)
                    + fit_extra_input
                )
                chat_out = (
                    int(getattr(chat_usage, "output_tokens", 0) or 0)
                    + fit_extra_output
                )
                chat_cache_read = int(
                    getattr(chat_usage, "cache_read_tokens", 0) or 0
                )
                chat_cache_write = int(
                    getattr(chat_usage, "cache_write_tokens", 0) or 0
                )

                voice = dispatch.voice
                duration_ms = int(
                    (datetime.now(UTC) - turn_started_at).total_seconds() * 1000
                )
                if self.engagement_id is not None:
                    await persist_turn(
                        bot=self.bot,
                        engagement_id=self.engagement_id,
                        request_id=request_id,
                        turn_kind="initial" if first_activation else "followup",
                        output_kind=(
                            "send_response"
                            if output.response is not None
                            else "no_response"
                        ),
                        triggering_messages=triggering,
                        agent_output=output.model_dump(mode="json"),
                        new_model_messages=list(result.new_messages()),
                        duration_ms=duration_ms,
                        chat_tokens_input=chat_in,
                        chat_tokens_output=chat_out,
                        chat_model_name=resolved_model_name,
                        chat_reasoning_level=resolved_reasoning_wire,
                        chat_cache_read_tokens=chat_cache_read,
                        chat_cache_write_tokens=chat_cache_write,
                        voice_tokens_input=(
                            voice.tokens_input if voice else 0
                        ),
                        voice_tokens_output=(
                            voice.tokens_output if voice else 0
                        ),
                        voice_model_name=voice.model_name if voice else None,
                        voice_sent_ok=voice.sent_ok if voice else None,
                        voice_send_error=voice.error if voice else None,
                        compaction_events=compaction_events,
                    )
            except Exception:
                logger.exception(
                    "[%s] Failed to persist chat agent turn", request_id
                )

            # Now that the turn is persisted, honour any pending deactivation.
            if dispatch.deactivate_reason:
                logger.info(
                    "Engine for channel %s deactivating: %s",
                    self.channel_id,
                    dispatch.deactivate_reason,
                )
                await self._deactivate(
                    send_notes_clear=True,
                    reason=dispatch.deactivate_reason,
                )
                return True

        # After releasing run_lock, re-check fire conditions immediately.
        await self._maybe_refire()
        return True

    async def _apply_output(
        self, output: TurnDecision, images: list[GeneratedImage] | None = None
    ) -> _TurnDispatchOutcome:
        """Write memory + dispatch sends. Returns the dispatch outcome so the
        engine can persist the turn BEFORE finalising any deactivation."""
        memory = get_chat_memory()
        outcome = _TurnDispatchOutcome()
        images = images or []

        await memory.write_topic(self.channel_id, output.topic)
        if output.notes is not None:
            try:
                await memory.write_notes(self.channel_id, output.notes)
            except Exception:
                logger.exception(
                    "Failed to persist chat notes for channel %s", self.channel_id
                )

        if output.response is not None:
            self.consecutive_no_response = 0
            self.last_sent_at = datetime.now(UTC)
            outcome.voice = await self._send(output.response, images)
        else:
            # No textual reply this turn. If the agent still generated an image,
            # post it standalone — that counts as engagement, so it must NOT burn
            # a no-response strike. Only a turn that delivered nothing is silence.
            posted = await self._post_images(images, reply_to=None) if images else False
            if posted:
                self.consecutive_no_response = 0
                self.last_sent_at = datetime.now(UTC)
            else:
                self.consecutive_no_response += 1
                if self.consecutive_no_response >= MAX_NO_RESPONSE_TURNS:
                    outcome.deactivate_reason = "no_response_quota"
                    return outcome

        if not output.continue_watching:
            outcome.deactivate_reason = "continue_watching_false"

        return outcome

    async def _send(
        self, body: ResponseBody, images: list[GeneratedImage] | None = None
    ) -> _VoiceOutcome | None:
        """Dispatch the agent's send outputs.

        Returns the voice outcome (sent_ok + usage + error) when voice was
        attempted, None otherwise — so the engine can record it on the turn.
        Any ``images`` ride along on the text reply; if there's no text this
        turn they're posted as their own message so they still reach the channel.
        """
        images = images or []
        reply_to: int | None = None
        if (
            body.reply_directly
            and body.target_message_id
            and body.target_message_id.isdigit()
        ):
            reply_to = int(body.target_message_id)

        has_text = bool(body.message and body.message.strip())
        has_voice = bool(body.voice_summary and body.voice_summary.strip())

        text_ok = True
        voice_outcome: _VoiceOutcome | None = None

        if not has_text and not has_voice and not images:
            return None

        # Dispatch in parallel; capture each result independently.
        async def _text_runner() -> bool:
            return await self._send_text(body.message, reply_to, images)

        async def _voice_runner() -> _VoiceOutcome:
            return await self._send_voice(
                body.voice_summary, reply_to, body.voice_instruction
            )

        tasks: dict[str, asyncio.Task] = {}
        if has_text:
            tasks["text"] = asyncio.create_task(_text_runner())
        elif images:
            # No text to carry the image — post it on its own (best-effort).
            tasks["images"] = asyncio.create_task(
                self._post_images(images, reply_to)
            )
        if has_voice:
            tasks["voice"] = asyncio.create_task(_voice_runner())

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for kind, res in zip(tasks.keys(), results):
            if kind == "text":
                if isinstance(res, BaseException) or res is False:
                    text_ok = False
            elif kind == "voice":
                if isinstance(res, BaseException):
                    voice_outcome = _VoiceOutcome(
                        sent_ok=False,
                        error=f"{type(res).__name__}: {res}",
                    )
                else:
                    voice_outcome = res
            # "images" is a best-effort standalone post — nothing to record.

        # Voice-only failed → tell the user so silence doesn't look like
        # the agent ignored them.
        if (
            has_voice
            and voice_outcome is not None
            and not voice_outcome.sent_ok
            and not has_text
        ):
            await self._post_error(
                "Couldn't send a voice message in this channel — give the bot "
                "SEND_VOICE_MESSAGES permission, or ask me to reply in text.",
                reply_to=reply_to,
            )

        return voice_outcome

    async def _send_text(
        self,
        message: str,
        reply_to: int | None,
        images: list[GeneratedImage] | None = None,
    ) -> bool:
        # A reply over Discord's 2000-char cap goes out as two messages, split
        # at the last newline before the 1500-char mark (see split_for_discord).
        # The reply anchor rides on the first message; any images ride on the
        # LAST message so an attachment never visually interrupts the text.
        parts = split_for_discord(message) or [""]
        attachments = (
            [hikari.Bytes(img.data, img.filename, img.mime_type) for img in images]
            if images
            else []
        )
        last_index = len(parts) - 1
        for index, part in enumerate(parts):
            sent = await self._send_message_part(
                part,
                reply_to=reply_to if index == 0 else None,
                attachments=attachments if index == last_index else [],
            )
            if not sent:
                # A failed lead means the reply didn't land. A failed
                # continuation is logged only — the lead already delivered
                # the reply's opening.
                return index > 0
        return True

    async def _send_message_part(
        self,
        content: str,
        reply_to: int | None,
        attachments: list[hikari.Bytes],
    ) -> bool:
        """Send one message of a (possibly split) reply."""
        base_kwargs: dict[str, Any] = {"content": content}
        if reply_to is not None:
            base_kwargs["reply"] = reply_to
        try:
            kwargs = dict(base_kwargs)
            if attachments:
                kwargs["attachments"] = attachments
            await self.bot.rest.create_message(self.channel_id, **kwargs)
            return True
        except Exception as err:
            if not attachments:
                logger.exception(
                    "Failed to send chat agent text in channel %s", self.channel_id
                )
                return False
            # The image upload failed — most often the bot lacks the "Attach
            # Files" permission here (or the file is too big). Don't lose the
            # whole reply: resend the text alone so the answer still lands, and
            # note the missing permission when that's the cause.
            logger.warning(
                "Send with %d attachment(s) failed in channel %s (%s); "
                "retrying text-only",
                len(attachments),
                self.channel_id,
                type(err).__name__,
                exc_info=True,
            )
            text_only = dict(base_kwargs)
            if isinstance(err, hikari.ForbiddenError):
                text_only["content"] = (content + _ATTACH_PERM_NOTE)[:2000]
            try:
                await self.bot.rest.create_message(self.channel_id, **text_only)
                return True
            except Exception:
                logger.exception(
                    "Failed to send chat agent text (text-only fallback) in "
                    "channel %s",
                    self.channel_id,
                )
                return False

    async def _post_images(
        self, images: list[GeneratedImage], reply_to: int | None
    ) -> bool:
        """Post generated images as their own channel message (no text body)."""
        if not images:
            return False
        try:
            kwargs: dict[str, Any] = {
                "attachments": [
                    hikari.Bytes(img.data, img.filename, img.mime_type)
                    for img in images
                ]
            }
            if reply_to is not None:
                kwargs["reply"] = reply_to
            await self.bot.rest.create_message(self.channel_id, **kwargs)
            return True
        except Exception as err:
            logger.exception(
                "Failed to post generated image(s) in channel %s", self.channel_id
            )
            # No text body to fall back to — but if it's a permission problem,
            # say so instead of going silent.
            if isinstance(err, hikari.ForbiddenError):
                try:
                    note_kwargs: dict[str, Any] = {"content": _ATTACH_PERM_NOTE.strip()}
                    if reply_to is not None:
                        note_kwargs["reply"] = reply_to
                    await self.bot.rest.create_message(self.channel_id, **note_kwargs)
                except Exception:
                    logger.debug(
                        "could not post attach-permission note in channel %s",
                        self.channel_id,
                        exc_info=True,
                    )
            return False

    def _override_service(self) -> Any | None:
        """The bot's ModelOverrideService (set on ``bot.d``), or None if absent.

        Mirrors ``plugins/model_override._get_override_service`` but stays
        fail-soft: a bot without the service (or a non-dict ``bot.d`` in tests)
        yields None so channels behave as if they have no override.
        """
        data = getattr(self.bot, "d", None)
        if not isinstance(data, dict):
            return None
        service = data.get("model_override_service")
        if service is None:
            service = data.get("_services", {}).get("model_override_service")
        return service

    def _budget_redis(self) -> Any | None:
        """The bot's shared Redis client used for per-channel token budgets.

        Reuses the chat-memory Redis connection on ``bot.d`` — enforcement runs
        bot-side, so we spend budget locally rather than round-tripping the API.
        Returns None when Redis is unavailable (budgets then simply don't block).
        """
        data = getattr(self.bot, "d", None)
        if not isinstance(data, dict):
            return None
        return data.get("chat_memory_redis")

    async def _get_channel_override(self) -> Any | None:
        """Read this channel's admin model override (fail-soft).

        A bad or unreachable override read must never break chat, so any error
        degrades to "no override" (default model, no budget) with a warning.
        """
        service = self._override_service()
        if service is None:
            return None
        try:
            return await service.get_override(
                str(self.guild_id), str(self.channel_id)
            )
        except APIError:
            logger.warning(
                "Failed to read model override for channel %s — using default",
                self.channel_id,
                exc_info=True,
            )
            return None

    def _resolve_override_model_id(self, override: Any | None) -> str | None:
        """Wire model id for ``override``, or None to use the default agent.

        A ``model_key`` of ``None`` means the override pins no model (budgets/
        behaviour only — the channel keeps the server default). A stored
        ``model_key`` the catalog no longer knows (stale) falls back to the
        default model with a warning rather than crashing the turn.
        """
        if override is None or override.model_key is None:
            return None
        catalog_model = get_model(override.model_key)
        if catalog_model is None:
            logger.warning(
                "Channel %s override names unknown model_key %r — "
                "falling back to default model",
                self.channel_id,
                override.model_key,
            )
            return None
        return catalog_model.model_id

    async def _resolve_temporary_default(
        self, redis: Any
    ) -> tuple[str | None, str | None]:
        """(wire model id, reasoning level) from the temporary bot-wide default
        override, or ``(None, None)`` when none is active.

        A stored ``model_key`` the catalog no longer knows degrades to the
        configured default with a warning, mirroring
        :meth:`_resolve_override_model_id`.
        """
        temporary_default = await read_default_model_override(redis)
        if temporary_default is None:
            return None, None
        catalog_model = get_model(temporary_default.model_key)
        if catalog_model is None:
            logger.warning(
                "Temporary default override names unknown model_key %r — "
                "using the configured default model",
                temporary_default.model_key,
            )
            return None, None
        return catalog_model.model_id, temporary_default.reasoning_level

    def _fallback_flag_key(self) -> str:
        """Redis key for this channel's active free-fallback flag."""
        return fallback_flag_key(str(self.channel_id))

    def _fallback_ended_key(self) -> str:
        """Redis key for this channel's "notify when the primary returns" marker."""
        return fallback_ended_key(str(self.channel_id))

    async def _fallback_window_active(self, redis: Any) -> bool:
        """Whether the free-fallback flag is set for this channel (best-effort).

        A member sets the flag to opt the channel into its fallback model while
        the primary's budget is spent; it self-expires at the budget reset. Any
        Redis trouble reads as "no fallback" so a bad read never silently swaps
        which model runs.
        """
        try:
            return bool(await redis.exists(self._fallback_flag_key()))
        except RedisError:
            logger.debug(
                "could not read fallback flag for channel %s",
                self.channel_id,
                exc_info=True,
            )
            return False

    def _resolve_fallback_model_id(self, override: Any | None) -> str | None:
        """Wire model id for the override's fallback model while the free-fallback
        window is active.

        A missing or stale ``fallback_model_key`` degrades to the primary
        override model (with a warning), mirroring ``_resolve_override_model_id``.
        """
        fallback_key = (
            getattr(override, "fallback_model_key", None)
            if override is not None
            else None
        )
        if fallback_key is None:
            logger.warning(
                "Channel %s fallback window active but no fallback model "
                "configured — using the primary override model",
                self.channel_id,
            )
            return self._resolve_override_model_id(override)
        catalog_model = get_model(fallback_key)
        if catalog_model is None:
            logger.warning(
                "Channel %s fallback names unknown model_key %r — using the "
                "primary override model",
                self.channel_id,
                fallback_key,
            )
            return self._resolve_override_model_id(override)
        return catalog_model.model_id

    @staticmethod
    def _to_gate_message(message: Any) -> GateMessage:
        """Convert a hikari message into a ``GateMessage`` for the response gate."""
        author = getattr(message, "author", None)
        author_display = ""
        if author is not None:
            author_display = (
                getattr(author, "username", None)
                or getattr(author, "display_name", None)
                or ""
            )
        return GateMessage(
            message_id=str(message.id),
            author_display=author_display,
            content=getattr(message, "content", None) or "",
        )

    async def _fetch_gate_grounding(self, candidates: list[Any]) -> list[GateMessage]:
        """Fetch up to ``GATE_GROUNDING_LIMIT`` channel messages preceding the
        gate ``candidates`` as read-only context (oldest-first).

        Any fetch failure degrades to empty grounding (debug log) — the gate
        still judges the candidates, just without their surrounding context.
        """
        candidate_ids = [int(message.id) for message in candidates if message is not None]
        if not candidate_ids:
            return []
        oldest_candidate_id = min(candidate_ids)
        fetched: list[Any] = []
        try:
            iterator = self.bot.rest.fetch_messages(
                self.channel_id, before=oldest_candidate_id
            ).limit(GATE_GROUNDING_LIMIT)
            async for message in iterator:
                fetched.append(message)
        except Exception:
            logger.debug(
                "could not fetch response-filter grounding for channel %s",
                self.channel_id,
                exc_info=True,
            )
            return []
        fetched.reverse()
        return [self._to_gate_message(message) for message in fetched]

    async def _fetch_gate_channel_name(self) -> str | None:
        """The channel's name for the response gate — a forum post's title.

        Best-effort: any lookup failure degrades to None (debug log) so the
        gate judges without the name rather than erroring the turn.
        """
        try:
            info = await fetch_channel_info(self.bot, self.channel_id)
        except Exception:
            logger.debug(
                "could not fetch channel name for response-filter gate in "
                "channel %s",
                self.channel_id,
                exc_info=True,
            )
            return None
        return info.get("channel_name") or None

    async def _gate_allows(
        self, response_filter: str, candidates: list[Any]
    ) -> set[str]:
        """Return the ids of ``candidates`` the response filter allows.

        Wraps the (already fail-open) message gate so an unexpected error still
        runs the turn unfiltered — a wasted reply is far cheaper than a channel
        that goes mute. ``candidates`` are hikari messages; grounding is up to
        five channel messages immediately preceding them plus the channel name
        (a forum post's title), so short follow-ups are judged in context.
        """
        candidate_messages = [
            self._to_gate_message(message)
            for message in candidates
            if message is not None
        ]
        if not candidate_messages:
            return set()
        grounding = await self._fetch_gate_grounding(candidates)
        channel_name = await self._fetch_gate_channel_name()
        try:
            allowed = await filter_messages(
                response_filter,
                candidate_messages,
                grounding,
                channel_name=channel_name,
            )
        except Exception:
            logger.warning(
                "response filter gate errored for channel %s — running turn "
                "unfiltered",
                self.channel_id,
                exc_info=True,
            )
            return {message.message_id for message in candidate_messages}
        return set(allowed)

    async def _maybe_notice_primary_restored(
        self, redis: Any, override: Any
    ) -> None:
        """Announce the primary model is answering again once a fallback window
        has ended, clearing the one-shot marker.

        The marker outlives the fallback flag by a day so the channel still
        learns its primary is back even when the first post-reset turn is much
        later. Best-effort: any Redis trouble simply skips the notice.
        """
        try:
            cleared = await redis.delete(self._fallback_ended_key())
        except RedisError:
            logger.debug(
                "could not read fallback-ended marker for channel %s",
                self.channel_id,
                exc_info=True,
            )
            return
        if not cleared:
            return
        model = (
            get_model(override.model_key)
            if override.model_key is not None
            else None
        )
        label = (
            model.label
            if model is not None
            else override.model_key or "the default model"
        )
        await self._post_notice(
            f"Budget reset — **{label}** is answering again.",
            reply_to=None,
        )

    async def _record_budget_usage(
        self, redis: Any, tokens: int, *, fallback_active: bool
    ) -> None:
        """Add ``tokens`` to the channel's usage windows (best-effort).

        A free-fallback turn's spend is metered into display-only windows
        (:func:`add_fallback_usage`) so it still shows in ``/bot-usage`` but
        never counts toward the enforced budget — otherwise the "free" opt-in
        would push the enforced day window over its cap and re-block the primary
        the moment the fallback window closes.
        """
        record = add_fallback_usage if fallback_active else add_usage
        try:
            await record(redis, str(self.channel_id), tokens)
        except RedisError:
            logger.warning(
                "Failed to record token budget usage for channel %s",
                self.channel_id,
                exc_info=True,
            )

    async def _charge_directed_messages(
        self,
        output: TurnDecision,
        drained: list[hikari.Message],
        *,
        first_activation: bool,
    ) -> None:
        """Count this turn's bot-directed messages against their authors' limits.

        Every ranked message scoring >= DIRECTED_SCORE_THRESHOLD was (per the
        agent) aimed at the bot, so it costs its author one slot in the rolling
        per-user message limit — this is what makes in-session follow-ups
        count toward the limit, not just @mention engagements. Members are
        message ids, so a mention already charged at the gate never
        double-counts. Best-effort: Redis trouble must never break the turn.
        """
        redis = self._budget_redis()
        if redis is None:
            return
        turn_messages = list(drained)
        if first_activation and self.activation_message is not None:
            turn_messages.append(self.activation_message)
        author_and_epoch_by_message_id: dict[str, tuple[str, float]] = {}
        for message in turn_messages:
            author = getattr(message, "author", None)
            if author is None or getattr(author, "is_bot", False):
                continue
            created_at = getattr(message, "created_at", None)
            sent_epoch = (
                created_at.timestamp()
                if created_at is not None
                else datetime.now(UTC).timestamp()
            )
            author_and_epoch_by_message_id[str(message.id)] = (
                str(author.id),
                sent_epoch,
            )
        charges_by_user: dict[str, dict[str, float]] = {}
        for ranking in output.rankings:
            if ranking.score < DIRECTED_SCORE_THRESHOLD:
                continue
            charged = author_and_epoch_by_message_id.get(ranking.message_id)
            if charged is None:
                continue
            user_id, sent_epoch = charged
            charges_by_user.setdefault(user_id, {})[ranking.message_id] = sent_epoch
        for user_id, message_epochs in charges_by_user.items():
            try:
                warnings = await record_directed_messages(
                    redis, user_id, message_epochs
                )
            except RedisError:
                logger.warning(
                    "Failed to record message-limit charges for user %s",
                    user_id,
                    exc_info=True,
                )
                continue
            for warning in warnings or ():
                try:
                    await self.bot.rest.create_message(
                        self.channel_id,
                        content=format_usage_warning_notice(user_id, warning),
                        user_mentions=[int(user_id)],
                    )
                except Exception:
                    logger.exception(
                        "Failed to send %s%% message-limit warning to user %s",
                        warning.percentage,
                        user_id,
                    )

    async def _maybe_notice_budget_exhausted(
        self,
        redis: Any,
        *,
        reset_epoch: int,
        reply_to: int | None,
        fallback_model_key: str | None = None,
    ) -> None:
        """Post the budget-exhausted notice, throttled to once per hour.

        The notice embeds ``reset_epoch`` as a Discord relative timestamp so
        readers see a live countdown to the budget reset. A Redis ``SET NX EX``
        acts as the per-channel throttle: only the call that wins the key posts,
        so repeated over-budget turns stay silent until the cooldown lapses.
        Any Redis hiccup skips the notice (never blocks). When
        ``fallback_model_key`` names a configured fallback, the notice carries a
        one-press button that opts the channel into that model for free.
        """
        try:
            won = await redis.set(
                f"modelbudget-notice:{self.channel_id}",
                "1",
                nx=True,
                ex=BUDGET_NOTICE_COOLDOWN_SECONDS,
            )
        except RedisError:
            logger.debug(
                "could not claim budget-notice throttle for channel %s",
                self.channel_id,
                exc_info=True,
            )
            return
        if won:
            notice = _BUDGET_EXHAUSTED_NOTICE_TEMPLATE.format(
                reset_tag=f"<t:{reset_epoch}:R>"
            )
            components = self._build_fallback_offer(fallback_model_key, reset_epoch)
            await self._post_notice(
                notice, reply_to=reply_to, components=components
            )

    def _build_fallback_offer(
        self, fallback_model_key: str | None, reset_epoch: int
    ) -> list[Any] | None:
        """One-button action row offering the configured fallback model, or None.

        ``None`` when no fallback is configured (or its key is stale) so the
        notice posts plain. The button's ``custom_id`` carries only the reset
        epoch; the handler reads channel/guild from the interaction.
        """
        if fallback_model_key is None:
            return None
        fallback_model = get_model(fallback_model_key)
        if fallback_model is None:
            return None
        row = hikari.impl.MessageActionRowBuilder()
        row.add_interactive_button(
            hikari.ButtonStyle.PRIMARY,
            f"{MODEL_BUDGET_FALLBACK_CUSTOM_ID_PREFIX}:{reset_epoch}",
            label=f"Answer with {fallback_model.label} instead"[:80],
        )
        return [row]

    def _shared_api_client(self) -> Any | None:
        """The bot's shared APIClient (set on ``bot.d``), or None if absent.

        Reused for the image-quota calls — both the per-turn read here and the
        ``generate_image`` tool via ``ChatDeps.api_client`` — so we don't build
        (and leak) a fresh HTTP client every turn.
        """
        return getattr(self.bot, "d", {}).get("api_client")

    async def _fetch_image_quota(self) -> dict | None:
        """Read this guild's remaining image budget for the prompt (best-effort).

        Surfaced to the agent as ``<image-quota>`` in the per-turn metadata so it
        knows up front how many technical images it can still draw this hour.
        Any failure degrades to no tag rather than blocking the turn.
        """
        api = self._shared_api_client()
        if api is None:
            return None
        try:
            resp = await api.get(
                "/image-generations/quota",
                params={"guild_id": str(self.guild_id)},
            )
            if resp.status_code < 400:
                return resp.json()
        except Exception:
            logger.debug(
                "could not fetch image quota for guild %s",
                self.guild_id,
                exc_info=True,
            )
        return None

    async def _send_voice(
        self,
        voice_summary: str,
        reply_to: int | None,
        instruction: str | None,
    ) -> _VoiceOutcome:
        try:
            usage = await self.voice_send(
                self.channel_id, voice_summary.strip(), reply_to, instruction
            )
        except Exception as e:
            logger.exception(
                "Voice send failed for channel %s", self.channel_id
            )
            return _VoiceOutcome(sent_ok=False, error=f"{type(e).__name__}: {e}")
        # The voice_send callback returns a TTSUsage (from VoiceService) when
        # successful. Older callers may return None — handle both.
        tokens_in = int(getattr(usage, "tokens_input", 0) or 0)
        tokens_out = int(getattr(usage, "tokens_output", 0) or 0)
        model = getattr(usage, "model_name", None) or None
        return _VoiceOutcome(
            sent_ok=True,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            model_name=model,
        )

    async def _post_error(self, text: str, *, reply_to: int | None = None) -> None:
        """Send a brief, user-facing error message to the channel.

        Best-effort: failures are swallowed (we don't want a cascading
        exception when the channel itself is the problem).
        """
        try:
            kwargs: dict[str, Any] = {"content": text[:2000]}
            if reply_to is not None:
                kwargs["reply"] = reply_to
            await self.bot.rest.create_message(self.channel_id, **kwargs)
        except Exception:
            logger.debug("Failed to post error message", exc_info=True)

    async def _post_notice(
        self,
        text: str,
        *,
        reply_to: int | None = None,
        components: list[Any] | None = None,
    ) -> None:
        """Send a channel notice, optionally carrying interactive components.

        Like :meth:`_post_error` but with component support (the budget-exhausted
        notice's fallback-offer button). Best-effort: send failures are swallowed.
        """
        try:
            kwargs: dict[str, Any] = {"content": text[:2000]}
            if reply_to is not None:
                kwargs["reply"] = reply_to
            if components is not None:
                kwargs["components"] = components
            await self.bot.rest.create_message(self.channel_id, **kwargs)
        except Exception:
            logger.debug("Failed to post notice message", exc_info=True)

    async def _maybe_refire(self) -> None:
        async with self.queue_lock:
            queued = len(self.queue)
        if queued == 0:
            return
        if queued >= QUEUE_FIRE_THRESHOLD:
            self.fire_event.set()
        else:
            self._schedule_idle_fire()


def _extract_tokens(usage: RunUsage | None) -> int:
    if usage is None:
        return 0
    return (usage.input_tokens or 0) + (usage.output_tokens or 0)
