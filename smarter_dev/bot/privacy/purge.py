"""The bot's purge consumer: remove one person from the agents' memory.

Reads :data:`PURGE_STREAM` in the ``smarter-dev-bot`` group. For each guild of
a command it rewrites, with the agents' own models (``privacy.compaction``):

- every chat-agent channel memory (``chat_agent:{ch}:history|topic|notes``)
  of the guild's channels, under the guild's chat privacy lock (a turn that
  starts meanwhile defers before reading history) and the ``run_lock`` of the
  engine registered at fold time (a turn in flight finishes first). The
  history is replaced only if it is still byte-for-byte what was folded, else
  it is folded again. A key whose channel the bot cannot place in any guild
  is purged too (once per command);
- unless the guild's proactive agent is external (the worker owns those,
  legacy keys and epoch included): the embedded guild history
  ``proactive:guild-history:{g}`` under the guild's ``wake_lock``, replacing
  the in-memory ``runner.history``; legacy per-channel
  ``proactive:{ch}:history`` keys of the guild's channels; and the watch
  instructions (keep / rewrite / drop per entry, via the settings API).

The proactive stores are rewritten under the guild's shared privacy lock
(``SET NX EX 600``, renewed, released by token), which embedded wakes and the
worker also respect; notifications queued for the guild before the purge are
discarded and the person's buffered watcher messages dropped. Proactive stores
are written only when the bot knows it owns them (not external, runtime
present, a legacy key's guild known or no external guild configured).

A store is folded unless it is provably clean (no id or name hit anywhere and
every member-written part attributed, ``privacy.attribution``) or this run
already finished it (``privacy:v1:purge-done:bot:{run_id}``, 30 days). A
redelivered run folds every store it has not finished and re-posts the acks
of guilds it has; a new run re-inspects everything. Unreadable stores are
reported failed and never touched.

Then it bumps ``purge_epoch_key(g)`` if a proactive history was rewritten,
posts the guild's ack (``<step>_name_hits=N`` per step in the detail), and
keeps the entry claimed (XCLAIM every 60 s) while it works. Once every guild's
ack was accepted (or the payload is malformed) it XACKs its own group and
XDELs only when every other group has read and acked the entry; a run the web
app answers 404 for is XDELed. Errors never stop the consumer: each entry is
guarded, the loop backs off, and a supervisor restarts it; every iteration
refreshes ``privacy:v1:consumer:bot:{host-pid}``. Logs carry run ids, guild
and channel ids and error type names only.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import socket
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import Model

from smarter_dev.bot import leadership
from smarter_dev.bot.privacy.attribution import chat_history_attributed
from smarter_dev.bot.privacy.attribution import part_texts
from smarter_dev.bot.privacy.attribution import proactive_history_attributed
from smarter_dev.bot.privacy.blocked_users import consumer_key
from smarter_dev.bot.privacy.compaction import PrivacyCompactionFailed
from smarter_dev.bot.privacy.compaction import purge_chat_memory
from smarter_dev.bot.privacy.compaction import purge_proactive_history
from smarter_dev.bot.privacy.compaction import purge_watch_instructions
from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.bot.services.chat_memory import chat_privacy_lock_key
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.privacy_purge import BOT_CONSUMER_GROUP
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import WORKER_CONSUMER_GROUP
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

# Ack-detail step names (``<step>_name_hits=N``, shared format with the worker).
STEP_NAMES = {
    "chat channels": "chat",
    "proactive guild history": "history",
    "legacy channel histories": "legacy_history",
    "watch instructions": "watch_instructions",
}

# Shared with the proactive-agent worker: whoever purges a guild's proactive
# stores holds it, so the two never write the same guild at once, and an
# embedded wake does not start while it exists.
PRIVACY_LOCK_TTL_SECONDS = 600
PRIVACY_LOCK_RENEW_SECONDS = 60
PRIVACY_LOCK_POLL_SECONDS = 1.0
PRIVACY_LOCK_WAIT_SECONDS = 600
# Per run: which stores (and guilds) this run already finished, so a
# redelivery of the same run re-posts acks instead of re-folding. A new run
# (resubmission) re-inspects every store.
DONE_TTL_SECONDS = 30 * 24 * 60 * 60
# Folds of one chat channel before giving up when its history keeps changing.
CHAT_WRITE_ATTEMPTS = 3
# How often a purge re-claims the stream entry it is working on, so a long
# purge is never reclaimed (idle > 10 min) by another consumer.
CLAIM_RENEW_SECONDS = 60
# Consumer liveness, read by the web page: SET on every loop iteration.
CONSUMER_HEARTBEAT_TTL_SECONDS = 180
# Backoff after the loop hits an error (Redis down, a bug).
ERROR_BACKOFF_SECONDS = 5.0

_RELEASE_LOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)
_RENEW_LOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)

_DONE = "done"
_STARTED = "started"
_SEEN_FIELD = "seen"
# Groups that must have finished with an entry before it may be deleted.
EXPECTED_OTHER_GROUPS = frozenset({WORKER_CONSUMER_GROUP})


def privacy_lock_key(guild_id: str) -> str:
    return f"proactive:v1:{{guild:{guild_id}}}:privacy-lock"


def purge_done_key(run_id: str) -> str:
    return f"privacy:v1:purge-done:bot:{run_id}"


class PrivacyLockBusy(Exception):
    """Another purger held the guild's privacy lock for too long."""


class OwnershipUnknown(Exception):
    """The bot cannot tell whether it owns a proactive store; it writes none."""


@dataclass
class PurgeDeps:
    """What a purge touches, injected so tests run without Discord."""

    redis: Any
    chat_memory: ChatMemory | None
    # Live chat engines (objects with channel_id, guild_id and run_lock).
    chat_engines: Callable[[], Awaitable[list[Any]]]
    # The engine registered for one channel right now, or None.
    chat_engine: Callable[[int], Awaitable[Any | None]]
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
    """State shared by every guild of one run."""

    target: PurgeTarget
    redis: Any = None
    run_id: str = ""
    # This run was delivered before (its done record already existed): any
    # store it has not finished is folded again, whatever the skip rule says.
    redelivered: bool = False
    # Channels whose guild is unknown, already handled under an earlier
    # guild, per store kind ("chat", "proactive").
    unplaced_done: dict[str, set[int]] = field(
        default_factory=lambda: {"chat": set(), "proactive": set()}
    )

    async def begin(self) -> None:
        """Note this delivery; learn whether the run was seen before."""
        if self.redis is None or not self.run_id:
            return
        key = purge_done_key(self.run_id)
        created = await self.redis.hsetnx(key, _SEEN_FIELD, "1")
        await self.redis.expire(key, DONE_TTL_SECONDS)
        self.redelivered = not created

    async def status(self, store: str) -> str | None:
        if self.redis is None or not self.run_id:
            return None
        value = await self.redis.hget(purge_done_key(self.run_id), store)
        return None if value is None else _decode(value)

    async def mark(self, store: str, value: str = _DONE) -> None:
        if self.redis is None or not self.run_id:
            return
        key = purge_done_key(self.run_id)
        await self.redis.hset(key, store, value)
        await self.redis.expire(key, DONE_TTL_SECONDS)

    async def recorded_ack(self, guild_id: str) -> PurgeAck | None:
        """This run's recorded ack for a guild; a corrupt record counts as
        absent (the guild is inspected again rather than poisoning the
        entry)."""
        raw = await self.status(f"guild:{guild_id}")
        if raw is None:
            return None
        try:
            return PurgeAck.model_validate_json(raw)
        except ValueError:
            logger.warning(
                "privacy purge run=%s guild=%s: unreadable done record ignored",
                self.run_id,
                guild_id,
            )
            return None


@dataclass
class _Report:
    """Content-free counts for one guild's ack."""

    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    stores: set[str] = field(default_factory=set)
    # Whole-word name matches the folds kept after their re-ask, per step.
    name_hits: dict[str, int] = field(default_factory=dict)
    errors: set[str] = field(default_factory=set)
    # A proactive history was rewritten: only then does the epoch move.
    proactive_written: bool = False
    # Listed names too short to check deterministically (PurgeTarget).
    unchecked_names: int = 0

    def add_name_hits(self, area: str, hits: int) -> None:
        self.name_hits[area] = self.name_hits.get(area, 0) + hits

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
        """The structured ack (Ack v1): the web decides from the fields; the
        detail is display text only (counts and error types, no content)."""
        outcome = "failed" if self.failed else "purged" if self.purged else "unchanged"
        parts = []
        for area in sorted(self.counts):
            values = " ".join(
                f"{key.replace(' ', '_')}={value}"
                for key, value in sorted(self.counts[area].items())
            )
            parts.append(f"{area} {values}")
        if self.errors:
            parts.append(f"errors: {', '.join(sorted(self.errors))}")
        detail = ""
        for segment in parts:
            candidate = f"{detail}; {segment}" if detail else segment
            if len(candidate) > 500:
                break
            detail = candidate
        return PurgeAck(
            component="bot",
            guild_id=guild_id,
            outcome=outcome,
            stores=sorted(self.stores),
            detail=detail,
            # One entry per step that ran.
            name_hits={
                STEP_NAMES[area]: self.name_hits.get(area, 0)
                for area in sorted(self.counts)
                if area in STEP_NAMES
            },
            tombstoned=False,  # the bot holds no tombstones
            unchecked_names=self.unchecked_names,
            # process_entry sets it once it knows whether the record was saved.
            done_record="not_written",
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
    """Every text a stored history carries: system prompt, member input,
    replies, tool calls and returns. All of it is searched for the person."""
    return [
        text
        for message in history
        for part in message.parts
        for text in part_texts(part)
    ]


def _mentions_target(texts: list[str], target: PurgeTarget) -> bool:
    return any(target.id_hits(text) or target.name_hits(text) for text in texts)


def proactive_history_is_clean(
    history: list[ModelMessage], target: PurgeTarget
) -> bool:
    """True when folding cannot remove anything: no part names the person,
    and every member-written part is attributed (``privacy.attribution``)."""
    if _mentions_target(_part_texts(history), target):
        return False
    return proactive_history_attributed(history)


def chat_memory_is_clean(
    history: list[ModelMessage],
    topic: str | None,
    notes: str | None,
    target: PurgeTarget,
) -> bool:
    """The chat-memory version. The system prompt and other host-written
    parts are only searched; member input must be attributed."""
    # Chat input is XML: search the unescaped text, so a nickname holding
    # `"`, `&` or `<` (stored as &quot; &amp; &lt;) is still found.
    texts = [
        *(html.unescape(text) for text in _part_texts(history)),
        topic or "",
        notes or "",
    ]
    if _mentions_target(texts, target):
        return False
    return chat_history_attributed(history)


class _GuildPrivacyLock:
    """``SET NX EX`` on a guild's privacy lock, renewed while held and
    released only by its own token."""

    def __init__(self, redis: Any, guild_id: str, *, key: str | None = None):
        self._redis = redis
        self._key = key or privacy_lock_key(guild_id)
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
                log_exception(logger, "privacy lock renewal failed", level=logging.WARNING)

    async def __aexit__(self, *exc_info) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
        await self._redis.eval(_RELEASE_LOCK, 1, self._key, self._token)


def _belongs(
    channel_id: int,
    guild_id: str,
    kind: str,
    deps: PurgeDeps,
    scope: _CommandScope,
    known: dict[int, str],
) -> bool:
    """Whether a stored channel is purged under this guild. A channel the
    cache cannot place is handled once per run and store kind."""
    owner = known.get(channel_id) or deps.channel_guild(channel_id)
    if owner is not None:
        return owner == guild_id
    done = scope.unplaced_done[kind]
    if channel_id in done:
        return False
    done.add(channel_id)
    return True


async def _skip(scope: _CommandScope, field_name: str) -> tuple[bool, bool]:
    """(already done in this run, must fold even if clean)."""
    status = await scope.status(field_name)
    if status == _DONE:
        return True, False
    # A redelivered run folds everything it has not finished: a store may
    # have been partly written (history yes, notes no) and look clean.
    return False, scope.redelivered


# -- chat agent ---------------------------------------------------------------


async def _purge_chat_channel(
    channel_id: int,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    done_field = f"chat_agent:{channel_id}"
    already, force = await _skip(scope, done_field)
    if already:
        report.count("chat channels", "already done")
        return
    # Looked up now, not from a snapshot: an engine created since the purge
    # started must have its running turn finish first. Engines created later
    # see the guild's chat privacy lock and defer before reading history.
    engine = await deps.chat_engine(channel_id)
    lock = engine.run_lock if engine is not None else contextlib.nullcontext()
    async with lock:
        for _attempt in range(CHAT_WRITE_ATTEMPTS):
            folded = await _fold_chat_channel(
                channel_id, deps, scope, report, force=force
            )
            if folded is not _CHANGED_UNDERNEATH:
                return
            logger.info(
                "privacy purge: chat history of channel %s changed while folding; "
                "folding again",
                channel_id,
            )
        logger.warning(
            "privacy purge: chat history of channel %s kept changing; left untouched",
            channel_id,
        )
        report.count("chat channels", "failed")
        report.stores.add(STORE_CHAT_HISTORY)


_CHANGED_UNDERNEATH = object()


async def _fold_chat_channel(
    channel_id: int,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
    *,
    force: bool,
) -> object | None:
    """One read-fold-write pass; ``_CHANGED_UNDERNEATH`` when the history
    changed between the read and the write (nothing written then)."""
    memory = deps.chat_memory
    done_field = f"chat_agent:{channel_id}"
    raw_history = await memory.read_history_raw(channel_id)
    history: list[ModelMessage] = []
    if raw_history:
        try:
            history = list(ModelMessagesTypeAdapter.validate_json(raw_history))
        except ValueError as error:
            # Never delete it (that would be a reset) and never log the
            # validation text (it quotes the stored content).
            logger.warning(
                "privacy purge: chat history of channel %s unreadable (%s); "
                "left untouched",
                channel_id,
                type(error).__name__,
            )
            report.count("chat channels", "failed")
            report.stores.add(STORE_CHAT_HISTORY)
            return None
    # The stored text, whatever its timestamp (a missing or malformed
    # topic_ts hides the topic from turns, not from the purge).
    try:
        topic = await memory.get_topic_text(channel_id)
        notes = await memory.get_notes_text(channel_id)
    except UnicodeDecodeError as error:
        logger.warning(
            "privacy purge: chat topic or notes of channel %s unreadable (%s); "
            "left untouched",
            channel_id,
            type(error).__name__,
        )
        report.count("chat channels", "failed")
        return None
    if not history and not topic and not notes:
        report.count("chat channels", "unchanged")
        await scope.mark(done_field)
        return None
    if not force and chat_memory_is_clean(history, topic, notes, scope.target):
        report.count("chat channels", "unchanged")
        await scope.mark(done_field)
        return None
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
        return None
    await scope.mark(done_field, _STARTED)
    if history:
        if raw_history is None or not await memory.replace_history_if_unchanged(
            channel_id, raw_history, result.history
        ):
            return _CHANGED_UNDERNEATH
        report.stores.add(STORE_CHAT_HISTORY)
    if topic and result.topic is not None:
        await memory.replace_topic(channel_id, result.topic)
        report.stores.add(STORE_CHAT_TOPIC)
    if notes and result.notes is not None:
        await memory.replace_notes(channel_id, result.notes)
        report.stores.add(STORE_CHAT_NOTES)
    report.add_name_hits("chat channels", result.name_hits)
    report.count("chat channels", "purged")
    await scope.mark(done_field)
    return None


async def _purge_chat(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    if deps.chat_memory is None:
        return
    async with _GuildPrivacyLock(
        deps.redis, guild_id, key=chat_privacy_lock_key(guild_id)
    ):
        engines = {
            int(engine.channel_id): engine
            for engine in await deps.chat_engines()
            if engine is not None
        }
        known = {
            channel: str(engine.guild_id) for channel, engine in engines.items()
        }
        candidates: set[int] = set(engines)
        for suffix in _CHAT_KEY_SUFFIXES:
            candidates |= await _channel_ids_with_keys(
                deps.redis, "chat_agent", suffix
            )
        for channel_id in sorted(candidates):
            if not _belongs(channel_id, guild_id, "chat", deps, scope, known):
                continue
            await _purge_chat_channel(channel_id, deps, scope, report)
        # Engines hold the guild memory blocks they read at activation (before
        # the guild memory was purged): drop them, re-read on the next turn.
        for engine in await deps.chat_engines():
            if engine is not None and str(engine.guild_id) == guild_id:
                invalidate = getattr(engine, "invalidate_guild_memory", None)
                if invalidate is not None:
                    invalidate()


# -- embedded proactive agent -------------------------------------------------


def _parse_history(raw: bytes | None) -> list[ModelMessage]:
    """Raises ValueError for unreadable bytes (never logged)."""
    if not raw:
        return []
    return list(ModelMessagesTypeAdapter.validate_json(raw))


async def _fold_proactive_history(
    *,
    area: str,
    store_name: str,
    done_field: str,
    read_raw: Callable[[], Awaitable[bytes | None]],
    in_memory: list[ModelMessage] | None,
    write: Callable[[list[ModelMessage]], Awaitable[None]],
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
    on_unreadable: Callable[[], None] | None = None,
) -> list[ModelMessage] | None:
    """Fold one proactive history; returns the new history when written."""
    already, force = await _skip(scope, done_field)
    if already:
        report.count(area, "already done")
        return None
    if in_memory is not None:
        history = in_memory
    else:
        try:
            history = _parse_history(await read_raw())
        except ValueError as error:
            logger.warning(
                "privacy purge: %s unreadable (%s); left untouched",
                area,
                type(error).__name__,
            )
            report.count(area, "failed")
            report.stores.add(store_name)
            if on_unreadable is not None:
                on_unreadable()
            return None
    if not history or (
        not force and proactive_history_is_clean(history, scope.target)
    ):
        report.count(area, "unchanged")
        await scope.mark(done_field)
        return None
    try:
        new_history, name_hits = await purge_proactive_history(
            history, scope.target, model=deps.proactive_model()
        )
    except PrivacyCompactionFailed:
        logger.warning("privacy purge: %s left untouched (no valid rewrite)", area)
        report.count(area, "failed")
        report.stores.add(store_name)
        return None
    await scope.mark(done_field, _STARTED)
    await write(new_history)
    report.proactive_written = True
    report.stores.add(store_name)
    report.add_name_hits(area, name_hits)
    report.count(area, "purged")
    await scope.mark(done_field)
    return new_history


async def _purge_proactive_guild_history(
    guild_id: str,
    state: Any | None,
    store: ProactiveHistoryStore,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    runner = getattr(state, "agent_runner", None) if state is not None else None

    def unreadable() -> None:
        # The stored bytes are what count. Make the runtime re-read them
        # before its next wake (and so not write its RAM copy over them).
        if state is not None:
            state.history_loaded = False

    async def write(new_history: list[ModelMessage]) -> None:
        await store.write_guild(int(guild_id), new_history, keep_clock=True)
        if runner is not None:
            runner.history = new_history

    await _fold_proactive_history(
        area="proactive guild history",
        store_name=STORE_PROACTIVE_GUILD_HISTORY,
        done_field=f"proactive:guild-history:{guild_id}",
        read_raw=lambda: store.read_guild_raw(int(guild_id)),
        in_memory=None,
        write=write,
        on_unreadable=unreadable,
        deps=deps,
        scope=scope,
        report=report,
    )


def _no_guild_is_external(run: Any) -> bool:
    from smarter_dev.bot.plugins.proactive import EXTERNAL_EXECUTION_MODE

    return (
        run.execution_mode != EXTERNAL_EXECUTION_MODE and not run.external_guild_ids
    )


async def _purge_legacy_channel_histories(
    guild_id: str,
    run: Any,
    store: ProactiveHistoryStore,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    area = "legacy channel histories"
    channels = await _channel_ids_with_keys(deps.redis, "proactive", "history")
    for channel_id in sorted(channels):
        placed = deps.channel_guild(channel_id) is not None
        if not _belongs(channel_id, guild_id, "proactive", deps, scope, {}):
            continue
        if not placed and not _no_guild_is_external(run):
            # It may belong to a guild the worker owns: never write it.
            logger.warning(
                "privacy purge: legacy proactive history of unplaced channel %s "
                "left to its owner (ownership unknown)",
                channel_id,
            )
            report.count(area, "failed")
            report.count(area, "owner unknown")
            continue

        async def write(new_history, channel_id=channel_id) -> None:
            await store.rewrite(channel_id, new_history)

        await _fold_proactive_history(
            area=area,
            store_name=STORE_PROACTIVE_CHANNEL_HISTORY,
            done_field=f"proactive:{channel_id}:history",
            read_raw=lambda channel_id=channel_id: store.read_raw(channel_id),
            in_memory=None,
            write=write,
            deps=deps,
            scope=scope,
            report=report,
        )


async def _purge_watch_instructions(
    guild_id: str, run: Any, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
    from smarter_dev.bot.proactive.environment import InstructionStore

    service = run.settings_service()
    if service is None:
        # Cannot read the stored instructions: a failed step, never a silent
        # "unchanged".
        report.count("watch instructions", "failed")
        report.errors.add("SettingsServiceUnavailable")
        return
    for row in await _all_watch_addenda(guild_id, run, service):
        done_field = f"watch_instructions:{guild_id}:{row.channel_id}"
        if await scope.status(done_field) == _DONE:
            report.count("watch instructions", "already done")
            continue
        store = InstructionStore.from_stored(
            OPERATING_POLICY_BRIEF, row.watch_addendum
        )
        if not any(scope.target.mentions(entry.text) for entry in store.entries):
            # Nothing names the person: no model call, bytes untouched.
            report.count("watch instructions", "unchanged")
            await scope.mark(done_field)
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
        report.add_name_hits("watch instructions", result.name_hits)
        if result.changed:
            store.entries = result.entries
            store.updates += 1
            await service.set_watch_addendum(
                guild_id, str(row.channel_id), store.to_stored()
            )
            report.stores.add(STORE_WATCH_INSTRUCTIONS)
        await scope.mark(done_field)


async def _all_watch_addenda(guild_id: str, run: Any, service: Any) -> list[Any]:
    """Every channel's stored watch instructions: the enabled channels, plus
    any disabled channel of the guild (from the bot cache) that still holds
    instructions — they are stored text too."""
    rows = list(await service.list_enabled_channels(guild_id))
    seen = {str(row.channel_id) for row in rows}
    view = getattr(run.bot.cache, "get_guild_channels_view_for_guild", None)
    channel_ids = [str(c) for c in (view(int(guild_id)) if view else {})]
    for channel_id in channel_ids:
        if channel_id in seen:
            continue
        settings = await service.get_settings(guild_id, channel_id)
        if settings.watch_addendum:
            rows.append(settings)
            seen.add(channel_id)
    return rows


def _clear_buffered_target_messages(run: Any, target: PurgeTarget) -> int:
    """Drop the person's buffered messages (and ones mentioning their id)
    from every channel's watcher buffer, keeping arrivals aligned."""
    removed = 0
    for state in run.channel_states.values():
        keep = [
            index
            for index, message in enumerate(state.buffer)
            if not (
                getattr(message, "author_id", None) == target.user_id
                or target.id_hits(getattr(message, "content", "") or "")
            )
        ]
        if len(keep) == len(state.buffer):
            continue
        removed += len(state.buffer) - len(keep)
        aligned = len(state.buffer_arrivals) == len(state.buffer)
        state.buffer = [state.buffer[index] for index in keep]
        if aligned:
            state.buffer_arrivals = [state.buffer_arrivals[index] for index in keep]
    return removed


async def _purge_proactive(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope, report: _Report
) -> None:
    from smarter_dev.bot.plugins.proactive import EXTERNAL_EXECUTION_MODE

    run = deps.proactive()
    if run is None:
        # Without the runtime the bot cannot tell who owns this guild's
        # proactive memory, so it writes none of it.
        report.count("proactive", "failed")
        report.errors.add(OwnershipUnknown.__name__)
        return
    if run.execution_mode_for(guild_id) == EXTERNAL_EXECUTION_MODE:
        # The worker owns this guild's proactive memory, including migrating
        # and purging the legacy keys the embedded runtime left behind, and
        # it bumps the purge epoch itself.
        report.count("proactive", "owned by the worker")
        return
    store = ProactiveHistoryStore(deps.redis)
    state = run.guild_states.get(int(guild_id))
    lock = state.wake_lock if state is not None else contextlib.nullcontext()
    async with contextlib.AsyncExitStack() as held:
        await held.enter_async_context(lock)
        await held.enter_async_context(_GuildPrivacyLock(deps.redis, guild_id))
        # A watcher call already in flight for one of the guild's channels
        # would enqueue its summary after the drain below: wait for it.
        for channel_state in list(run.channel_states.values()):
            if channel_state.guild_id == guild_id:
                await held.enter_async_context(channel_state.processing_lock)
        await _purge_proactive_locked(
            guild_id, run, state, store, deps, scope, report
        )


async def _purge_proactive_locked(
    guild_id: str,
    run: Any,
    state: Any | None,
    store: ProactiveHistoryStore,
    deps: PurgeDeps,
    scope: _CommandScope,
    report: _Report,
) -> None:
    if state is not None:
        # Everything queued before the purge may carry the person's words
        # or name in a form no matcher knows; it has not reached the model
        # yet, so all of it is discarded. Later notifications are kept.
        _items, _dropped = state.queue.drain()
    _clear_buffered_target_messages(run, scope.target)
    await _purge_proactive_guild_history(
        guild_id, state, store, deps, scope, report
    )
    await _purge_legacy_channel_histories(
        guild_id, run, store, deps, scope, report
    )
    await _purge_watch_instructions(guild_id, run, deps, scope, report)
    if state is not None:
        # Re-read the (purged) guild memory bundle on the next wake.
        state.memory_refreshed_at = 0.0


async def purge_guild(
    guild_id: str, deps: PurgeDeps, scope: _CommandScope
) -> PurgeAck:
    """Purge one guild and return its ack (never raises)."""
    report = _Report(
        unchecked_names=len(scope.target.names) - len(scope.target.checked_names)
    )
    for step in (_purge_chat, _purge_proactive):
        try:
            await step(guild_id, deps, scope, report)
        except Exception as error:  # noqa: BLE001 — reported in the ack
            logger.warning(
                "privacy purge step %s failed guild=%s (%s)",
                step.__name__,
                guild_id,
                type(error).__name__,
            )
            report.errors.add(type(error).__name__)
    if report.proactive_written:
        try:
            await deps.redis.incr(purge_epoch_key(guild_id))
        except Exception as error:  # noqa: BLE001 — reported in the ack
            logger.warning(
                "privacy purge epoch bump failed guild=%s (%s)",
                guild_id,
                type(error).__name__,
            )
            report.errors.add(type(error).__name__)
    return report.ack(guild_id)


# -- stream consumer ----------------------------------------------------------


async def _keep_claimed(redis: Any, consumer: str, stream_id: Any) -> None:
    """Re-claim the entry every CLAIM_RENEW_SECONDS so its idle time never
    reaches the 10-minute reclaim while this consumer is still on it."""
    while True:
        await asyncio.sleep(CLAIM_RENEW_SECONDS)
        # A purge can outlast the heartbeat's 180 s: renew it here too.
        await _heartbeat(redis)
        try:
            await redis.xclaim(
                PURGE_STREAM,
                BOT_CONSUMER_GROUP,
                consumer,
                0,
                [stream_id],
                justid=True,
            )
        except Exception as error:  # noqa: BLE001 — next tick retries
            logger.warning(
                "privacy purge claim renewal failed entry=%s (%s)",
                _decode(stream_id),
                type(error).__name__,
            )


async def process_entry(
    deps: PurgeDeps, stream_id: Any, fields: dict, *, consumer: str | None = None
) -> None:
    """Handle one stream entry; never raises.

    XACK once every guild's ack was accepted. Any error leaves the entry
    pending, to be reclaimed and retried; with ``consumer`` set the entry
    stays claimed by it while it runs.
    """
    renewer = (
        asyncio.create_task(_keep_claimed(deps.redis, consumer, stream_id))
        if consumer is not None
        else None
    )
    try:
        await _process_entry(deps, stream_id, fields)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 — the entry is retried later
        logger.warning(
            "privacy purge entry=%s failed (%s); left pending for retry",
            _decode(stream_id),
            type(error).__name__,
        )
    finally:
        if renewer is not None:
            renewer.cancel()


async def _process_entry(deps: PurgeDeps, stream_id: Any, fields: dict) -> None:
    entry = _decode(stream_id)
    payload = fields.get(b"payload", fields.get("payload"))
    try:
        command = PurgeCommand.model_validate_json(
            payload if payload is not None else b""
        )
    except (ValidationError, ValueError, TypeError):
        # Never echo the payload: it may carry the target's id and names.
        logger.warning("privacy purge: malformed command skipped entry=%s", entry)
        await _finish_entry(deps.redis, stream_id)
        return
    run_id = str(command.run_id)
    scope = _CommandScope(
        target=PurgeTarget.build(command.user_id, command.names),
        redis=deps.redis,
        run_id=run_id,
    )
    await scope.begin()
    logger.info(
        "privacy purge run=%s started guilds=%d redelivered=%s",
        run_id,
        len(command.guild_ids),
        scope.redelivered,
    )
    all_acked = True
    for guild_id in command.guild_ids:
        ack = await scope.recorded_ack(guild_id)
        if ack is not None:
            ack = ack.model_copy(update={"done_record": "replayed"})
        else:
            ack = await purge_guild(guild_id, deps, scope)
            if ack.outcome != "failed":
                ack = ack.model_copy(update={"done_record": "written"})
                try:
                    await scope.mark(f"guild:{guild_id}", ack.model_dump_json())
                except Exception as error:  # noqa: BLE001 — still ack it
                    logger.warning(
                        "privacy purge run=%s guild=%s done record not saved (%s)",
                        run_id,
                        guild_id,
                        type(error).__name__,
                    )
                    ack = ack.model_copy(update={"done_record": "not_written"})
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
            logger.info("privacy purge run=%s unknown to the web app; skipped", run_id)
            await _finish_entry(deps.redis, stream_id, unknown_run=True)
            return
        logger.info(
            "privacy purge run=%s guild=%s outcome=%s", run_id, guild_id, ack.outcome
        )
    if all_acked:
        await _finish_entry(deps.redis, stream_id)


def _stream_id_key(stream_id: Any) -> tuple[int, int]:
    millis, _, sequence = _decode(stream_id).partition("-")
    return int(millis), int(sequence or 0)


async def _others_finished(redis: Any, stream_id: Any) -> bool:
    """Every other consumer group has read the entry and holds it pending
    nowhere (last-delivered-id >= entry, not in its XPENDING)."""
    entry = _stream_id_key(stream_id)
    groups = await redis.xinfo_groups(PURGE_STREAM)
    names = {_decode(group["name"]) for group in groups}
    if not EXPECTED_OTHER_GROUPS <= names:
        # The worker's group does not exist yet: it has not read the entry.
        return False
    for group in groups:
        name = _decode(group["name"])
        if name == BOT_CONSUMER_GROUP:
            continue
        if _stream_id_key(group["last-delivered-id"]) < entry:
            return False
        pending = await redis.xpending_range(
            PURGE_STREAM, name, min=stream_id, max=stream_id, count=1
        )
        if pending:
            return False
    return True


async def _finish_entry(
    redis: Any, stream_id: Any, *, unknown_run: bool = False
) -> None:
    """XACK for the bot's group; XDEL only when no other group still needs
    the entry (it carries the person's id and names), or when the web app
    no longer knows the run."""
    await redis.xack(PURGE_STREAM, BOT_CONSUMER_GROUP, stream_id)
    if unknown_run or await _others_finished(redis, stream_id):
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


async def _heartbeat(redis: Any) -> None:
    try:
        await redis.set(consumer_key(), "1", ex=CONSUMER_HEARTBEAT_TTL_SECONDS)
    except Exception as error:  # noqa: BLE001 — the key just expires
        logger.warning("privacy consumer heartbeat failed (%s)", type(error).__name__)


async def purge_consumer_loop(
    deps: PurgeDeps, *, block_ms: int = READ_BLOCK_MS
) -> None:
    """Consume purge commands, only while this process acts.

    The live chat engines and the embedded proactive runtime belong to the
    acting process; a standby holds none and must not rewrite their stores.
    Only the acting consumer heartbeats, and only after a successful poll;
    a missing group (NOGROUP) is recreated at id 0; any error backs off and
    the loop carries on.
    """
    consumer = f"{socket.gethostname()}-{id(deps)}"
    group_ready = False
    while True:
        try:
            if not leadership.is_acting():
                await asyncio.sleep(IDLE_SECONDS)
                continue
            if not group_ready:
                await ensure_group(deps.redis)
                group_ready = True
            try:
                entries = await read_batch(deps.redis, consumer, block_ms=block_ms)
            except Exception as error:
                if "NOGROUP" not in str(error):
                    raise
                logger.warning("privacy purge group missing; recreating it at 0")
                group_ready = False
                continue
            await _heartbeat(deps.redis)
            if not entries:
                # Real Redis blocked in XREADGROUP; never spin the loop.
                await asyncio.sleep(0)
            for stream_id, fields in entries:
                if not leadership.is_acting():
                    break
                await leadership.run_accepted(
                    process_entry(deps, stream_id, fields, consumer=consumer)
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — Redis blip or a bug
            logger.warning(
                "privacy purge consumer iteration failed (%s); backing off",
                type(error).__name__,
            )
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)


async def supervise(
    make: Callable[[], Awaitable[None]], *, backoff: float = ERROR_BACKOFF_SECONDS
) -> None:
    """Run ``make()`` forever, restarting it whenever it ends or dies."""
    while True:
        try:
            await make()
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 — restart, never die
            logger.warning(
                "privacy purge consumer died (%s); restarting", type(error).__name__
            )
        await asyncio.sleep(backoff)
