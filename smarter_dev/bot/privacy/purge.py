"""The bot's purge consumer: remove one person from the agents' memory.

Reads :data:`PURGE_STREAM` in the ``smarter-dev-bot`` group. For each guild of
a command it rewrites, with the agents' own models (``privacy.compaction``):

- every chat-agent channel memory (``chat_agent:{ch}:history|topic|notes``)
  of the guild's channels, under the live engine's ``run_lock`` so no turn in
  flight writes a stale history back. A key whose channel the bot cannot place
  in any guild is purged too (once per command);
- the embedded proactive agent's guild history ``proactive:guild-history:{g}``
  under the guild's ``wake_lock``, replacing the in-memory ``runner.history``;
- legacy per-channel ``proactive:{ch}:history`` keys of the guild's channels;
- the watch instructions of guilds whose proactive agent runs embedded here
  (keep / rewrite / drop per entry, persisted through the settings API).

Then it bumps ``purge_epoch_key(g)`` and posts the guild's ack. The entry is
XACKed once every guild's ack was accepted; a malformed payload is logged
without content, XACKed and dropped. A store whose rewrite fails stays exactly
as it was and the guild acks ``failed``. Logs carry run ids, guild and channel
ids only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pydantic import ValidationError
from pydantic_ai.models import Model

from smarter_dev.bot import leadership
from smarter_dev.bot.privacy.compaction import PrivacyCompactionFailed
from smarter_dev.bot.privacy.compaction import purge_chat_memory
from smarter_dev.bot.privacy.compaction import purge_proactive_history
from smarter_dev.bot.privacy.compaction import purge_watch_instructions
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.shared.privacy_purge import BOT_CONSUMER_GROUP
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import PurgeAck
from smarter_dev.shared.privacy_purge import PurgeCommand
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.privacy_purge import purge_epoch_key

logger = logging.getLogger(__name__)

RECLAIM_IDLE_MS = 10 * 60 * 1000
READ_BLOCK_MS = 5_000
IDLE_SECONDS = 1.0
READ_COUNT = 5

STORE_CHAT_HISTORY = "chat_agent:history"
STORE_CHAT_TOPIC = "chat_agent:topic"
STORE_CHAT_NOTES = "chat_agent:notes"
STORE_PROACTIVE_GUILD_HISTORY = "proactive:guild-history"
STORE_PROACTIVE_CHANNEL_HISTORY = "proactive:channel-history"
STORE_WATCH_INSTRUCTIONS = "watch_instructions"

_CHAT_KEY_SUFFIXES = ("history", "topic", "notes")


@dataclass
class PurgeDeps:
    """What a purge touches, injected so tests run without Discord."""

    redis: Any
    chat_memory: ChatMemory | None
    # Live chat engines (objects with channel_id, guild_id and run_lock).
    chat_engines: Callable[[], Awaitable[list[Any]]]
    # The guild a channel belongs to, from the bot cache; None when unknown.
    channel_guild: Callable[[int], str | None]
    # The embedded proactive runtime now, or None when the plugin is not
    # loaded (read at call time: the plugin may load or reload later).
    proactive: Callable[[], Any | None]
    chat_model: Callable[[], Model | str]
    proactive_model: Callable[[], Model | str]
    post_ack: Callable[[str, PurgeAck], Awaitable[bool]]


@dataclass
class _CommandScope:
    """State shared by every guild of one command."""

    target: PurgeTarget
    # Channels whose guild is unknown, already purged under an earlier guild.
    unplaced_done: set[int] = field(default_factory=set)


@dataclass
class _Report:
    """Content-free counts for one guild's ack."""

    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    stores: set[str] = field(default_factory=set)
    name_hits: int = 0
    errors: set[str] = field(default_factory=set)

    def count(self, area: str, result: str, amount: int = 1) -> None:
        self.counts.setdefault(area, {}).setdefault(result, 0)
        self.counts[area][result] += amount

    @property
    def failed(self) -> bool:
        return any(c.get("failed") for c in self.counts.values()) or bool(self.errors)

    @property
    def purged(self) -> bool:
        return any(
            c.get("purged") or c.get("rewritten") or c.get("dropped")
            for c in self.counts.values()
        )

    def ack(self, guild_id: str) -> PurgeAck:
        outcome = "failed" if self.failed else "purged" if self.purged else "unchanged"
        parts = []
        for area in sorted(self.counts):
            values = " ".join(
                f"{key}={value}" for key, value in sorted(self.counts[area].items())
            )
            parts.append(f"{area}: {values}")
        if self.name_hits:
            parts.append(f"name mentions kept after re-ask: {self.name_hits}")
        if self.errors:
            parts.append(f"errors: {', '.join(sorted(self.errors))}")
        detail = "; ".join(parts)[:500]
        return PurgeAck(
            component="bot",
            guild_id=guild_id,
            outcome=outcome,
            stores=sorted(self.stores),
            detail=detail,
        )


def _decode(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _channel_ids_with_keys(redis: Any, prefix: str, suffix: str) -> set[int]:
    """Channel ids of ``{prefix}:{channel}:{suffix}`` keys."""
    found: set[int] = set()
    async for key in redis.scan_iter(match=f"{prefix}:*:{suffix}"):
        parts = _decode(key).split(":")
        if len(parts) == 3 and parts[1].isdigit():
            found.add(int(parts[1]))
    return found


def _belongs(
    channel_id: int,
    guild_id: str,
    deps: PurgeDeps,
    scope: _CommandScope,
    known: dict[int, str],
) -> bool:
    """Whether a stored channel is purged under this guild."""
    owner = known.get(channel_id) or deps.channel_guild(channel_id)
    if owner is not None:
        return owner == guild_id
    if channel_id in scope.unplaced_done:
        return False
    scope.unplaced_done.add(channel_id)
    return True


# -- chat agent ---------------------------------------------------------------


async def _purge_chat_channel(
    channel_id: int,
    engine: Any | None,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    memory = deps.chat_memory
    lock = engine.run_lock if engine is not None else contextlib.nullcontext()
    async with lock:
        history = await memory.read_history(channel_id)
        topic_record = await memory.get_topic(channel_id)
        topic = topic_record.text if topic_record is not None else None
        notes = await memory.get_notes(channel_id)
        if not history and not topic and not notes:
            report.count("chat channels", "unchanged")
            return
        try:
            result = await purge_chat_memory(
                history, topic, notes, scope.target, model=deps.chat_model()
            )
        except PrivacyCompactionFailed:
            logger.warning(
                "privacy purge: chat memory of channel %s left untouched "
                "(no valid rewrite)",
                channel_id,
            )
            report.count("chat channels", "failed")
            if history:
                report.stores.add(STORE_CHAT_HISTORY)
            return
        if history:
            await memory.replace_history(channel_id, result.history)
            report.stores.add(STORE_CHAT_HISTORY)
        if topic and result.topic is not None:
            await memory.replace_topic(channel_id, result.topic)
            report.stores.add(STORE_CHAT_TOPIC)
        if notes and result.notes is not None:
            await memory.replace_notes(channel_id, result.notes)
            report.stores.add(STORE_CHAT_NOTES)
        report.name_hits += result.name_hits
        report.count("chat channels", "purged")


async def _purge_chat(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    if deps.chat_memory is None:
        return
    engines = {
        int(engine.channel_id): engine
        for engine in await deps.chat_engines()
        if engine is not None
    }
    known = {channel: str(engine.guild_id) for channel, engine in engines.items()}
    candidates: set[int] = set(engines)
    for suffix in _CHAT_KEY_SUFFIXES:
        candidates |= await _channel_ids_with_keys(deps.redis, "chat_agent", suffix)
    for channel_id in sorted(candidates):
        if not _belongs(channel_id, guild_id, deps, scope, known):
            continue
        await _purge_chat_channel(
            channel_id, engines.get(channel_id), deps, scope, report
        )


# -- embedded proactive agent -------------------------------------------------


async def _purge_proactive_guild_history(
    guild_id: str,
    state: Any | None,
    store: ProactiveHistoryStore,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    runner = getattr(state, "agent_runner", None) if state is not None else None
    if runner is not None and state.history_loaded:
        history = list(runner.history)
    else:
        history = await store.read_guild(int(guild_id))
    if not history:
        report.count("proactive guild history", "unchanged")
        return
    try:
        new_history, name_hits = await purge_proactive_history(
            history, scope.target, model=deps.proactive_model()
        )
    except PrivacyCompactionFailed:
        logger.warning(
            "privacy purge: proactive history of guild %s left untouched "
            "(no valid rewrite)",
            guild_id,
        )
        report.count("proactive guild history", "failed")
        report.stores.add(STORE_PROACTIVE_GUILD_HISTORY)
        return
    await store.write_guild(int(guild_id), new_history)
    if runner is not None:
        runner.history = new_history
    report.stores.add(STORE_PROACTIVE_GUILD_HISTORY)
    report.name_hits += name_hits
    report.count("proactive guild history", "purged")


async def _purge_legacy_channel_histories(
    guild_id: str,
    store: ProactiveHistoryStore,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    channels = await _channel_ids_with_keys(deps.redis, "proactive", "history")
    for channel_id in sorted(channels):
        if not _belongs(channel_id, guild_id, deps, scope, {}):
            continue
        history = await store.read(channel_id)
        if not history:
            report.count("legacy channel histories", "unchanged")
            continue
        try:
            new_history, name_hits = await purge_proactive_history(
                history, scope.target, model=deps.proactive_model()
            )
        except PrivacyCompactionFailed:
            logger.warning(
                "privacy purge: legacy proactive history of channel %s left "
                "untouched (no valid rewrite)",
                channel_id,
            )
            report.count("legacy channel histories", "failed")
            report.stores.add(STORE_PROACTIVE_CHANNEL_HISTORY)
            continue
        await store.write(channel_id, new_history)
        report.stores.add(STORE_PROACTIVE_CHANNEL_HISTORY)
        report.name_hits += name_hits
        report.count("legacy channel histories", "purged")


async def _purge_watch_instructions(
    guild_id: str, run: Any, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    from smarter_dev.bot.plugins.proactive import EXTERNAL_EXECUTION_MODE
    from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
    from smarter_dev.bot.proactive.environment import InstructionStore

    if run.execution_mode_for(guild_id) == EXTERNAL_EXECUTION_MODE:
        return  # the external worker owns this guild's instructions
    service = run.settings_service()
    if service is None:
        return
    rows = await service.list_enabled_channels(guild_id)
    for row in rows:
        store = InstructionStore.from_stored(
            OPERATING_POLICY_BRIEF, row.watch_addendum
        )
        if not store.entries:
            continue
        try:
            result = await purge_watch_instructions(
                store, scope.target, model=deps.proactive_model()
            )
        except PrivacyCompactionFailed:
            logger.warning(
                "privacy purge: watch instructions of channel %s left untouched "
                "(no valid rewrite)",
                row.channel_id,
            )
            report.count("watch instructions", "failed")
            report.stores.add(STORE_WATCH_INSTRUCTIONS)
            continue
        report.count("watch instructions", "kept", result.kept)
        report.count("watch instructions", "rewritten", result.rewritten)
        report.count("watch instructions", "dropped", result.dropped)
        report.name_hits += result.name_hits
        if result.changed:
            store.entries = result.entries
            store.updates += 1
            await service.set_watch_addendum(
                guild_id, str(row.channel_id), store.to_stored()
            )
            report.stores.add(STORE_WATCH_INSTRUCTIONS)


async def _purge_proactive(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    store = ProactiveHistoryStore(deps.redis)
    run = deps.proactive()
    state = run.guild_states.get(int(guild_id)) if run is not None else None
    lock = state.wake_lock if state is not None else contextlib.nullcontext()
    async with lock:
        # A wake queued before the list blocked the person may still carry
        # their words; it has not reached the model yet, so it is dropped.
        if state is not None:
            state.queue.items = [
                item
                for item in state.queue.items
                if not scope.target.id_hits(item.body)
            ]
        await _purge_proactive_guild_history(
            guild_id, state, store, deps, scope, report
        )
        await _purge_legacy_channel_histories(guild_id, store, deps, scope, report)
        if run is not None:
            await _purge_watch_instructions(guild_id, run, deps, scope, report)
        if state is not None:
            # Re-read the (purged) guild memory bundle on the next wake.
            state.memory_refreshed_at = 0.0


async def purge_guild(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope
) -> PurgeAck:
    """Purge one guild and return its ack (never raises)."""
    report = _Report()
    for step in (_purge_chat, _purge_proactive):
        try:
            await step(guild_id, deps, scope, report)
        except Exception as error:  # noqa: BLE001 — reported in the ack
            logger.exception(
                "privacy purge step %s failed guild=%s", step.__name__, guild_id
            )
            report.errors.add(type(error).__name__)
    try:
        await deps.redis.incr(purge_epoch_key(guild_id))
    except Exception as error:  # noqa: BLE001 — reported in the ack
        logger.exception("privacy purge epoch bump failed guild=%s", guild_id)
        report.errors.add(type(error).__name__)
    return report.ack(guild_id)


# -- stream consumer ----------------------------------------------------------


async def process_entry(deps: PurgeDeps, stream_id: Any, fields: dict) -> None:
    """Handle one stream entry; XACK when every guild's ack was accepted."""
    entry = _decode(stream_id)
    payload = fields.get(b"payload", fields.get("payload"))
    try:
        command = PurgeCommand.model_validate_json(
            payload if payload is not None else b""
        )
    except (ValidationError, ValueError, TypeError):
        # Never echo the payload: it may carry the target's id and names.
        logger.warning("privacy purge: malformed command dropped entry=%s", entry)
        await deps.redis.xack(PURGE_STREAM, BOT_CONSUMER_GROUP, stream_id)
        return
    run_id = str(command.run_id)
    scope = _CommandScope(
        target=PurgeTarget.build(command.user_id, command.names)
    )
    logger.info(
        "privacy purge run=%s started guilds=%d", run_id, len(command.guild_ids)
    )
    all_acked = True
    for guild_id in command.guild_ids:
        ack = await purge_guild(guild_id, deps, scope)
        try:
            accepted = await deps.post_ack(run_id, ack)
        except Exception as error:  # noqa: BLE001 — retried on reclaim
            logger.warning(
                "privacy purge run=%s guild=%s ack post failed (%s)",
                run_id,
                guild_id,
                type(error).__name__,
            )
            all_acked = False
            continue
        if not accepted:
            logger.info("privacy purge run=%s unknown to the web app; dropped", run_id)
            await deps.redis.xack(PURGE_STREAM, BOT_CONSUMER_GROUP, stream_id)
            return
        logger.info(
            "privacy purge run=%s guild=%s outcome=%s", run_id, guild_id, ack.outcome
        )
    if all_acked:
        await deps.redis.xack(PURGE_STREAM, BOT_CONSUMER_GROUP, stream_id)


async def ensure_group(redis: Any) -> None:
    try:
        await redis.xgroup_create(
            PURGE_STREAM, BOT_CONSUMER_GROUP, id="0", mkstream=True
        )
    except Exception as error:  # redis-py has sync/async ResponseError variants
        if "BUSYGROUP" not in str(error):
            raise


async def read_batch(redis: Any, consumer: str, *, block_ms: int = READ_BLOCK_MS):
    """Stalled entries first (idle > 10 min), then new ones."""
    reclaimed = await redis.xautoclaim(
        PURGE_STREAM,
        BOT_CONSUMER_GROUP,
        consumer,
        RECLAIM_IDLE_MS,
        "0-0",
        count=READ_COUNT,
    )
    if reclaimed and reclaimed[1]:
        return list(reclaimed[1])
    records = await redis.xreadgroup(
        BOT_CONSUMER_GROUP,
        consumer,
        {PURGE_STREAM: ">"},
        count=READ_COUNT,
        block=block_ms,
    )
    return [entry for _stream, entries in records or () for entry in entries]


async def purge_consumer_loop(deps: PurgeDeps) -> None:
    """Consume purge commands, only while this process acts.

    The live chat engines and the embedded proactive runtime belong to the
    acting process; a standby holds none and must not rewrite their stores.
    """
    await ensure_group(deps.redis)
    consumer = f"{socket.gethostname()}-{id(deps)}"
    while True:
        if not leadership.is_acting():
            await asyncio.sleep(IDLE_SECONDS)
            continue
        try:
            entries = await read_batch(deps.redis, consumer)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — Redis blip; try again shortly
            logger.exception("privacy purge stream read failed")
            await asyncio.sleep(IDLE_SECONDS)
            continue
        for stream_id, fields in entries:
            if not leadership.is_acting():
                break
            await leadership.run_accepted(process_entry(deps, stream_id, fields))
