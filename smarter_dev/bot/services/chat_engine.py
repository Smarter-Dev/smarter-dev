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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import hikari
from pydantic_ai.usage import RunUsage

from smarter_dev.bot.agents.chat_agent import (
    DEFAULT_MODEL as CHAT_DEFAULT_MODEL,
    MODEL_ENV_VAR as CHAT_MODEL_ENV_VAR,
    get_chat_agent,
)
from smarter_dev.bot.agents.chat_compaction import (
    drain_collection,
    start_collection,
)
from smarter_dev.bot.agents.chat_context import (
    build_followup_input,
    build_initial_input,
)
from smarter_dev.bot.agents.chat_input_format import build_agent_call
from smarter_dev.bot.agents.chat_models import ResponseBody, TurnDecision
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.services.chat_conversation_persistence import (
    end_engagement,
    persist_turn,
    start_engagement,
)
from smarter_dev.bot.services.chat_memory import get_chat_memory
from smarter_dev.bot.utils.stop_detection import (
    is_stop_request,
    set_channel_cooldown,
)

logger = logging.getLogger(__name__)

IDLE_FIRE_SECONDS = 5
QUEUE_FIRE_THRESHOLD = 15
MAX_NO_RESPONSE_TURNS = 3
INACTIVITY_TIMEOUT = timedelta(minutes=30)
MAX_RUNTIME = timedelta(hours=2)


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

                await self._run_once(first_activation=first_activation)
                first_activation = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chat engine crashed for channel %s", self.channel_id)
            await self._deactivate(send_notes_clear=True, reason="crash")

    async def _run_once(self, *, first_activation: bool) -> None:
        async with self.run_lock:
            turn_started_at = datetime.now(UTC)
            # Snapshot queue; new messages can keep arriving while we run.
            async with self.queue_lock:
                drained = [q.message for q in self.queue]
                self.queue.clear()

            # Best reply-to anchor for any user-facing error message: the
            # activation trigger on first turn, otherwise the freshest
            # queued message.
            error_reply_to: int | None = None
            if first_activation and self.activation_message is not None:
                error_reply_to = int(self.activation_message.id)
            elif drained:
                error_reply_to = int(drained[-1].id)

            memory = get_chat_memory()
            if first_activation:
                await memory.reset_idle_counter(self.channel_id)

            try:
                if first_activation:
                    trigger = self.activation_message
                    if trigger is None:
                        logger.error(
                            "Initial activation for channel %s has no "
                            "trigger_message set — aborting fire",
                            self.channel_id,
                        )
                        return
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
                        return
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
                return

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

            agent = get_chat_agent()
            deps = ChatDeps(
                bot=self.bot,
                channel_id=self.channel_id,
                guild_id=self.guild_id,
            )
            # Install a per-run compaction collector. The history processor
            # appends events to it; we drain after the run.
            start_collection()
            user_prompt, message_history = build_agent_call(agent_input, history)
            try:
                result = await agent.run(
                    user_prompt=user_prompt,
                    message_history=message_history,
                    deps=deps,
                )
            except Exception:
                logger.exception(
                    "[%s] Chat agent run failed for channel %s",
                    request_id,
                    self.channel_id,
                )
                drain_collection()  # discard
                await self._post_error(
                    "Sorry, couldn't generate a reply for that one — try again?",
                    reply_to=error_reply_to,
                )
                return
            compaction_events = drain_collection()

            output = result.output
            tokens = _extract_tokens(result.usage())
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

            dispatch = await self._apply_output(output)

            # Persist this turn to the operator dashboard (best-effort).
            try:
                triggering = (
                    [agent_input.activation_message.model_dump(mode="json")]
                    if first_activation
                    else [m.model_dump(mode="json") for m in agent_input.new_messages]
                )
                chat_usage = result.usage()
                chat_in = int(getattr(chat_usage, "input_tokens", 0) or 0)
                chat_out = int(getattr(chat_usage, "output_tokens", 0) or 0)
                chat_model = (
                    getattr(chat_usage, "requests", None)
                    and getattr(chat_usage, "model", None)
                    or os.environ.get(CHAT_MODEL_ENV_VAR, CHAT_DEFAULT_MODEL)
                )

                voice = dispatch.voice or _VoiceOutcome(sent_ok=False) if False else dispatch.voice
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
                        chat_model_name=chat_model,
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
                return

        # After releasing run_lock, re-check fire conditions immediately.
        await self._maybe_refire()

    async def _apply_output(
        self, output: TurnDecision
    ) -> _TurnDispatchOutcome:
        """Write memory + dispatch sends. Returns the dispatch outcome so the
        engine can persist the turn BEFORE finalising any deactivation."""
        memory = get_chat_memory()
        outcome = _TurnDispatchOutcome()

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
            outcome.voice = await self._send(output.response)
        else:
            self.consecutive_no_response += 1
            if self.consecutive_no_response >= MAX_NO_RESPONSE_TURNS:
                outcome.deactivate_reason = "no_response_quota"
                return outcome

        if not output.continue_watching:
            outcome.deactivate_reason = "continue_watching_false"

        return outcome

    async def _send(self, body: ResponseBody) -> _VoiceOutcome | None:
        """Dispatch the agent's send outputs.

        Returns the voice outcome (sent_ok + usage + error) when voice was
        attempted, None otherwise — so the engine can record it on the turn.
        """
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

        if not has_text and not has_voice:
            return None

        # Dispatch in parallel; capture each result independently.
        async def _text_runner() -> bool:
            return await self._send_text(body.message, reply_to)

        async def _voice_runner() -> _VoiceOutcome:
            return await self._send_voice(
                body.voice_summary, reply_to, body.voice_instruction
            )

        tasks: dict[str, asyncio.Task] = {}
        if has_text:
            tasks["text"] = asyncio.create_task(_text_runner())
        if has_voice:
            tasks["voice"] = asyncio.create_task(_voice_runner())

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for kind, res in zip(tasks.keys(), results):
            if kind == "text":
                if isinstance(res, BaseException) or res is False:
                    text_ok = False
            else:  # voice
                if isinstance(res, BaseException):
                    voice_outcome = _VoiceOutcome(
                        sent_ok=False,
                        error=f"{type(res).__name__}: {res}",
                    )
                else:
                    voice_outcome = res

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

    async def _send_text(self, message: str, reply_to: int | None) -> bool:
        try:
            kwargs: dict[str, Any] = {"content": message.strip()[:2000]}
            if reply_to is not None:
                kwargs["reply"] = reply_to
            await self.bot.rest.create_message(self.channel_id, **kwargs)
            return True
        except Exception:
            logger.exception(
                "Failed to send chat agent text in channel %s", self.channel_id
            )
            return False

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
