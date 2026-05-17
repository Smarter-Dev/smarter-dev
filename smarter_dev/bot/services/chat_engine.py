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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import hikari
from pydantic_ai.usage import RunUsage

from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_context import build_agent_input
from smarter_dev.bot.agents.chat_models import NoResponse, SendResponse
from smarter_dev.bot.agents.chat_tools import ChatDeps
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
    message_id: int
    author_id: int
    content: str
    enqueued_at: datetime


@dataclass
class ChannelEngine:
    """Drives the chat agent for a single channel."""

    bot: Any  # hikari.GatewayBot; Any keeps tests light
    channel_id: int
    guild_id: int
    voice_send: Callable[[int, str, int | None], Awaitable[None]]
    on_deactivate: Callable[[int], Awaitable[None]]

    queue: list[_QueuedMessage] = field(default_factory=list)
    queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fire_event: asyncio.Event = field(default_factory=asyncio.Event)

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    consecutive_no_response: int = 0
    active: bool = True
    _idle_task: asyncio.Task | None = None
    _runner_task: asyncio.Task | None = None
    _shutdown: bool = False

    # ------------------------------------------------------------------
    # Public API

    def start(self) -> None:
        """Begin the background runner. Call once after construction."""
        if self._runner_task is None:
            self._runner_task = asyncio.create_task(self._runner())

    def trigger_initial(self) -> None:
        """Fire the agent immediately with the most-recent channel context.

        Called once when the engine is first created by an @mention/reply.
        Queue is left empty; the agent works off the live channel history.
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
                    message_id=int(event.message.id),
                    author_id=int(event.author.id),
                    content=content,
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
        await self._deactivate(send_notes_clear=True)

    async def _deactivate(self, *, send_notes_clear: bool) -> None:
        if not self.active:
            return
        self.active = False
        try:
            if send_notes_clear:
                await get_chat_memory().clear_notes(self.channel_id)
        except Exception:
            logger.exception("Failed to clear notes for channel %s", self.channel_id)
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
                    await self._deactivate(send_notes_clear=True)
                    break

                if datetime.now(UTC) - self.started_at > MAX_RUNTIME:
                    logger.info(
                        "Engine for channel %s hit max runtime — deactivating",
                        self.channel_id,
                    )
                    await self._deactivate(send_notes_clear=True)
                    break

                await self._run_once(first_activation=first_activation)
                first_activation = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chat engine crashed for channel %s", self.channel_id)
            await self._deactivate(send_notes_clear=True)

    async def _run_once(self, *, first_activation: bool) -> None:
        async with self.run_lock:
            # Drain queue snapshot; new messages can keep arriving.
            async with self.queue_lock:
                self.queue.clear()

            memory = get_chat_memory()
            # Reset the idle counter as soon as we engage — the agent is active now.
            if first_activation:
                await memory.reset_idle_counter(self.channel_id)

            try:
                agent_input = await build_agent_input(
                    bot=self.bot,
                    channel_id=self.channel_id,
                    guild_id=self.guild_id,
                    memory=memory,
                    include_notes=not first_activation,
                )
            except Exception:
                logger.exception(
                    "Failed to build agent input for channel %s", self.channel_id
                )
                return

            request_id = uuid.uuid4().hex[:8]
            logger.info(
                "[%s] Chat agent firing channel=%s first=%s messages=%d",
                request_id,
                self.channel_id,
                first_activation,
                len(agent_input.messages),
            )

            agent = get_chat_agent()
            deps = ChatDeps(
                bot=self.bot,
                channel_id=self.channel_id,
                guild_id=self.guild_id,
            )
            try:
                result = await agent.run(
                    user_prompt=agent_input.model_dump_json(),
                    deps=deps,
                )
            except Exception:
                logger.exception(
                    "[%s] Chat agent run failed for channel %s",
                    request_id,
                    self.channel_id,
                )
                return

            output = result.output
            tokens = _extract_tokens(result.usage())
            logger.info(
                "[%s] Chat agent returned kind=%s continue_watching=%s tokens=%s",
                request_id,
                output.kind,
                output.continue_watching,
                tokens,
            )

            await self._apply_output(output)

        # After releasing run_lock, re-check fire conditions immediately.
        await self._maybe_refire()

    async def _apply_output(self, output: NoResponse | SendResponse) -> None:
        memory = get_chat_memory()

        await memory.write_topic(self.channel_id, output.topic)

        if isinstance(output, SendResponse):
            self.consecutive_no_response = 0
            self.last_sent_at = datetime.now(UTC)
            try:
                await memory.write_notes(self.channel_id, output.notes)
            except Exception:
                logger.exception(
                    "Failed to persist chat notes for channel %s", self.channel_id
                )

            await self._send(output)
        else:
            self.consecutive_no_response += 1
            if self.consecutive_no_response >= MAX_NO_RESPONSE_TURNS:
                logger.info(
                    "Engine for channel %s hit %d consecutive no-response turns — deactivating",
                    self.channel_id,
                    MAX_NO_RESPONSE_TURNS,
                )
                await self._deactivate(send_notes_clear=True)
                return

        if not output.continue_watching:
            logger.info(
                "Agent requested deactivation for channel %s", self.channel_id
            )
            await self._deactivate(send_notes_clear=True)

    async def _send(self, output: SendResponse) -> None:
        """Dispatch the agent's send outputs.

        Text and voice are independent channels; either, both, or (by validator)
        at least one is present. When both are present they're dispatched in
        parallel.
        """
        reply_to: int | None = None
        if output.reply_to_message_id and output.reply_to_message_id.isdigit():
            reply_to = int(output.reply_to_message_id)

        tasks: list[asyncio.Task] = []
        if output.message and output.message.strip():
            tasks.append(
                asyncio.create_task(self._send_text(output.message, reply_to))
            )
        if output.voice_summary and output.voice_summary.strip():
            tasks.append(
                asyncio.create_task(self._send_voice(output.voice_summary, reply_to))
            )

        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_text(self, message: str, reply_to: int | None) -> None:
        try:
            kwargs: dict[str, Any] = {"content": message.strip()[:2000]}
            if reply_to is not None:
                kwargs["reply"] = reply_to
            await self.bot.rest.create_message(self.channel_id, **kwargs)
        except Exception:
            logger.exception(
                "Failed to send chat agent text in channel %s", self.channel_id
            )

    async def _send_voice(self, voice_summary: str, reply_to: int | None) -> None:
        try:
            await self.voice_send(self.channel_id, voice_summary.strip(), reply_to)
        except Exception:
            logger.exception(
                "Voice send failed for channel %s", self.channel_id
            )

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
