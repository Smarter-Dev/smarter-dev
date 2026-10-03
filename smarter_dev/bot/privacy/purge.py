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

The proactive stores are rewritten under the guild's shared privacy lock
(``SET NX EX 600``, renewed, released by token), which embedded wakes and the
worker also respect. A store is folded unless this purge request already
finished it (``privacy:v1:purge-done:bot:{request_id}``, 30 days) or it is
provably clean: every raw line attributed (``uid=`` / ``user-id=``) and no
hit on the id or a name. A guild already finished under the request has its
recorded ack re-posted under the current run id instead of being re-folded.

Then it bumps ``purge_epoch_key(g)`` and posts the guild's ack. The entry is
XACKed once every guild's ack was accepted; a malformed payload or a run the
web app does not know is logged without content, XACKed and XDELed. A store
whose rewrite fails stays exactly as it was and the guild acks ``failed``.
Logs carry run ids, guild and channel ids only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage
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

# Shared with the proactive-agent worker: whoever purges a guild's proactive
# stores holds it, so the two never write the same guild at once, and an
# embedded wake does not start while it exists.
PRIVACY_LOCK_TTL_SECONDS = 600
PRIVACY_LOCK_RENEW_SECONDS = 60
PRIVACY_LOCK_POLL_SECONDS = 1.0
PRIVACY_LOCK_WAIT_SECONDS = 600
# Which stores (and guilds) a purge REQUEST already finished, so a redelivery
# or a resubmitted run re-posts acks instead of re-folding other members'
# memory.
DONE_TTL_SECONDS = 30 * 24 * 60 * 60

_RELEASE_LOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)
_RENEW_LOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
# A transcript line as render_transcript_line writes it: "[time] [id=…] …".
_TRANSCRIPT_LINE_PREFIX = re.compile(r"^\[[^\]]+\] \[id=")


def privacy_lock_key(guild_id: str) -> str:
    return f"proactive:v1:{{guild:{guild_id}}}:privacy-lock"


def purge_done_key(request_id: str) -> str:
    return f"privacy:v1:purge-done:bot:{request_id}"


class PrivacyLockBusy(Exception):
    """Another purger held the guild's privacy lock for too long."""


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
    redis: Any = None
    request_id: str = ""
    # Channels whose guild is unknown, already purged under an earlier guild.
    unplaced_done: set[int] = field(default_factory=set)

    async def done(self, store: str) -> bool:
        """Whether this request already finished ``store``."""
        if self.redis is None or not self.request_id:
            return False
        return bool(await self.redis.hexists(purge_done_key(self.request_id), store))

    async def mark_done(self, store: str, value: str = "1") -> None:
        if self.redis is None or not self.request_id:
            return
        key = purge_done_key(self.request_id)
        await self.redis.hset(key, store, value)
        await self.redis.expire(key, DONE_TTL_SECONDS)

    async def done_value(self, store: str) -> str | None:
        if self.redis is None or not self.request_id:
            return None
        value = await self.redis.hget(purge_done_key(self.request_id), store)
        return None if value is None else _decode(value)


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
            c.get("purged")
            or c.get("rewritten")
            or c.get("dropped")
            or c.get("already done")
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


def _part_texts(history: list[ModelMessage]) -> list[str]:
    """Every text a stored history carries (prompts, replies, tool I/O)."""
    texts: list[str] = []
    for message in history:
        for part in message.parts:
            content = getattr(part, "content", None)
            if content is None:
                content = getattr(part, "args", None)
            if content is None:
                continue
            texts.append(content if isinstance(content, str) else str(content))
    return texts


def _mentions_target(texts: list[str], target: PurgeTarget) -> bool:
    return any(target.id_hits(text) or target.name_hits(text) for text in texts)


def proactive_history_is_clean(
    history: list[ModelMessage], target: PurgeTarget
) -> bool:
    """True when folding cannot remove anything: every raw transcript line
    carries ``uid=`` attribution and nothing names the target's id or names.

    A line without ``uid=`` (rendered before attribution existed) could be the
    target under a nickname the purge does not know, so it forces a fold.
    """
    texts = _part_texts(history)
    if _mentions_target(texts, target):
        return False
    for text in texts:
        for line in text.splitlines():
            if _TRANSCRIPT_LINE_PREFIX.match(line) and "(uid=" not in line:
                return False
    return True


def chat_memory_is_clean(
    history: list[ModelMessage],
    topic: str | None,
    notes: str | None,
    target: PurgeTarget,
) -> bool:
    """The chat-memory version: every ``<message`` tag is attributed
    (``user-id=`` or ``self=``) and nothing names the target."""
    texts = [*_part_texts(history), topic or "", notes or ""]
    if _mentions_target(texts, target):
        return False
    for text in texts:
        for chunk in text.split("<message")[1:]:
            tag = chunk.split(">", 1)[0]
            if 'user-id="' not in tag and 'self="true"' not in tag:
                return False
    return True


class _GuildPrivacyLock:
    """``SET NX EX`` on the guild's privacy lock, renewed while held and
    released only by its own token."""

    def __init__(self, redis: Any, guild_id: str):
        self._redis = redis
        self._key = privacy_lock_key(guild_id)
        self._token = uuid.uuid4().hex
        self._renewer: asyncio.Task | None = None

    async def __aenter__(self) -> _GuildPrivacyLock:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PRIVACY_LOCK_WAIT_SECONDS
        while not await self._redis.set(
            self._key, self._token, nx=True, ex=PRIVACY_LOCK_TTL_SECONDS
        ):
            if loop.time() >= deadline:
                raise PrivacyLockBusy("guild privacy lock held elsewhere")
            await asyncio.sleep(PRIVACY_LOCK_POLL_SECONDS)
        self._renewer = asyncio.create_task(self._renew())
        return self

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(PRIVACY_LOCK_RENEW_SECONDS)
            try:
                await self._redis.eval(
                    _RENEW_LOCK, 1, self._key, self._token, PRIVACY_LOCK_TTL_SECONDS
                )
            except Exception:  # noqa: BLE001 — the TTL still bounds the lock
                logger.warning("privacy lock renewal failed", exc_info=True)

    async def __aexit__(self, *exc_info) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
        await self._redis.eval(_RELEASE_LOCK, 1, self._key, self._token)


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
    done_field = f"chat_agent:{channel_id}"
    if await scope.done(done_field):
        report.count("chat channels", "already done")
        return
    lock = engine.run_lock if engine is not None else contextlib.nullcontext()
    async with lock:
        history = await memory.read_history(channel_id)
        topic_record = await memory.get_topic(channel_id)
        topic = topic_record.text if topic_record is not None else None
        notes = await memory.get_notes(channel_id)
        if not history and not topic and not notes:
            report.count("chat channels", "unchanged")
            return
        if chat_memory_is_clean(history, topic, notes, scope.target):
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
        await scope.mark_done(done_field)


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
    done_field = f"proactive:guild-history:{guild_id}"
    if await scope.done(done_field):
        report.count("proactive guild history", "already done")
        return
    runner = getattr(state, "agent_runner", None) if state is not None else None
    if runner is not None and state.history_loaded:
        history = list(runner.history)
    else:
        history = await store.read_guild(int(guild_id))
    if not history or proactive_history_is_clean(history, scope.target):
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
    await scope.mark_done(done_field)


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
        done_field = f"proactive:{channel_id}:history"
        if await scope.done(done_field):
            report.count("legacy channel histories", "already done")
            continue
        history = await store.read(channel_id)
        if not history or proactive_history_is_clean(history, scope.target):
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
        await scope.mark_done(done_field)


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
        done_field = f"watch_instructions:{guild_id}:{row.channel_id}"
        if await scope.done(done_field):
            report.count("watch instructions", "already done")
            continue
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
        await scope.mark_done(done_field)


async def _purge_proactive(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    store = ProactiveHistoryStore(deps.redis)
    run = deps.proactive()
    state = run.guild_states.get(int(guild_id)) if run is not None else None
    lock = state.wake_lock if state is not None else contextlib.nullcontext()
    async with lock, _GuildPrivacyLock(deps.redis, guild_id):
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
        await _drop(deps.redis, stream_id)
        return
    run_id = str(command.run_id)
    scope = _CommandScope(
        target=PurgeTarget.build(command.user_id, command.names),
        redis=deps.redis,
        request_id=str(command.request_id),
    )
    logger.info(
        "privacy purge run=%s started guilds=%d", run_id, len(command.guild_ids)
    )
    all_acked = True
    for guild_id in command.guild_ids:
        recorded = await scope.done_value(f"guild:{guild_id}")
        if recorded is not None:
            # Finished under this request already: re-post, do not re-fold.
            ack = PurgeAck.model_validate_json(recorded)
        else:
            ack = await purge_guild(guild_id, deps, scope)
            if ack.outcome != "failed":
                await scope.mark_done(f"guild:{guild_id}", ack.model_dump_json())
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
            await _drop(deps.redis, stream_id)
            return
        logger.info(
            "privacy purge run=%s guild=%s outcome=%s", run_id, guild_id, ack.outcome
        )
    if all_acked:
        await deps.redis.xack(PURGE_STREAM, BOT_CONSUMER_GROUP, stream_id)


async def _drop(redis: Any, stream_id: Any) -> None:
    """XACK and XDEL an entry nobody will ever process (it may carry the
    target's id and names)."""
    await redis.xack(PURGE_STREAM, BOT_CONSUMER_GROUP, stream_id)
    await redis.xdel(PURGE_STREAM, stream_id)


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
