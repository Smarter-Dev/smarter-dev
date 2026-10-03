"""One admin request to remove a Discord user from the chat bot, end to end.

The order is the point:

1. **Stop reading them.** Opening a request puts the user on the block list
   (:class:`~smarter_dev.web.models.ChatBotBlockedUser`). The run then waits
   until both runtimes report enforcing that list revision, so no wake in
   between can read their old Discord messages back into history.
2. **Guild memory.** The agent rewrites each guild's blocks, notes and
   revisions without them (:mod:`smarter_dev.web.chat_memory_purge`), one
   guild per transaction.
3. **Working history.** One :class:`~smarter_dev.shared.privacy_purge.PurgeCommand`
   goes to the bot and the external worker, which each run a forced privacy
   compaction over the histories they hold and acknowledge per guild.
4. **The check.** Once every guild is acknowledged, a deterministic search of
   every store touched reports where the ID or a name is still found. It does
   not take the agent's word for anything.

Every step is safe to repeat: a clean store comes back unchanged. Closing a
request strips the ID and names, leaving a bare receipt; the block-list row
stays, because it is what keeps the deletion true.

This module is imported by the web tier (admin page, bot API), so it must stay
light: the agent stack is imported only inside :func:`run_purge`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import RUNTIME_COMPONENTS
from smarter_dev.shared.privacy_purge import BlockedUsers
from smarter_dev.shared.privacy_purge import PurgeAck
from smarter_dev.shared.privacy_purge import PurgeCommand
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.privacy_purge import enforcing_key
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotBlockedUsersRevision
from smarter_dev.web.models import ChatBotPurgeRequest
from smarter_dev.web.models import ProactiveAgentHistory
from smarter_dev.web.models import ProactiveChannelSettings

logger = logging.getLogger(__name__)

# Request lifecycle, in order. ``needs_review`` and ``complete`` are both ends
# of a run; only ``closed`` strips the request to a receipt.
STATUS_QUEUED = "queued"
STATUS_WAITING = "waiting_for_runtimes"
STATUS_PURGING = "purging"
STATUS_AWAITING_ACKS = "awaiting_acks"
STATUS_CHECKING = "checking"
STATUS_COMPLETE = "complete"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_CLOSED = "closed"

BLOCK_SOURCE_PURGE = "purge"
MAX_NAMES = 20
# How long a run waits for both runtimes to enforce the new block list.
ENFORCING_WAIT_SECONDS = 600
ENFORCING_POLL_SECONDS = 5

SessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


# -- the block list ---------------------------------------------------------------


async def block_list_revision(session: AsyncSession) -> int:
    row = await session.get(ChatBotBlockedUsersRevision, 1)
    return row.revision if row is not None else 0


async def add_blocked_user(
    session: AsyncSession, discord_user_id: str, *, source: str = BLOCK_SOURCE_PURGE
) -> int:
    """Put ``discord_user_id`` on the block list; return the list's revision.

    Adding someone already listed changes nothing and returns the current
    revision, which is what makes a repeated request safe.
    """
    if await session.get(ChatBotBlockedUser, discord_user_id) is not None:
        return await block_list_revision(session)
    session.add(ChatBotBlockedUser(discord_user_id=discord_user_id, source=source))
    row = await session.get(ChatBotBlockedUsersRevision, 1, with_for_update=True)
    if row is None:
        row = ChatBotBlockedUsersRevision(id=1, revision=0)
        session.add(row)
    row.revision += 1
    await session.flush()
    return row.revision


async def read_blocked_users(session: AsyncSession) -> BlockedUsers:
    # Revision first: if a block commits between the two reads, the list is
    # newer than its revision (safe) rather than older (a runtime would report
    # enforcing a revision whose user it is still reading).
    revision = await block_list_revision(session)
    user_ids = (await session.scalars(select(ChatBotBlockedUser.discord_user_id))).all()
    return BlockedUsers(revision=revision, user_ids=sorted(user_ids))


async def runtimes_enforcing(redis) -> dict[str, int | None]:
    """The block-list revision each runtime last reported enforcing, if any."""
    values = await redis.mget([enforcing_key(c) for c in RUNTIME_COMPONENTS])
    enforcing: dict[str, int | None] = {}
    for component, raw in zip(RUNTIME_COMPONENTS, values, strict=True):
        try:
            enforcing[component] = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            enforcing[component] = None
    return enforcing


def all_enforcing(enforcing: dict[str, int | None], revision: int = 0) -> bool:
    return all(value is not None and value >= revision for value in enforcing.values())


# -- opening a request --------------------------------------------------------------


def clean_names(names: list[str]) -> list[str]:
    target = PurgeTarget.build("0", [n[:100] for n in names])
    return list(target.names[:MAX_NAMES])


async def open_purge_request(
    session: AsyncSession,
    *,
    discord_user_id: str,
    names: list[str],
    requested_by: str | None,
) -> ChatBotPurgeRequest:
    """Block the user and open (or restart) their purge request; the caller commits.

    A user with an open request gets that request back with a new run, so a
    second submit restarts the same purge instead of starting a parallel one.
    """
    revision = await add_blocked_user(session, discord_user_id)
    request = await session.scalar(
        select(ChatBotPurgeRequest)
        .where(
            ChatBotPurgeRequest.discord_user_id == discord_user_id,
            ChatBotPurgeRequest.status != STATUS_CLOSED,
        )
        .with_for_update()
    )
    if request is None:
        request = ChatBotPurgeRequest(
            discord_user_id=discord_user_id, requested_by=requested_by, names=[]
        )
        session.add(request)
    request.names = clean_names([*(request.names or []), *names])
    start_new_run(request, list_revision=revision)
    await session.flush()
    return request


def start_new_run(request: ChatBotPurgeRequest, *, list_revision: int) -> None:
    previous_entry = (request.steps or {}).get("command_entry_id")
    request.run_id = uuid4()
    request.status = STATUS_QUEUED
    request.completed_at = None
    request.check_report = None
    request.steps = {
        "list_revision": list_revision,
        "guild_ids": [],
        "memory": {},
        "bot": {},
        "worker": {},
        # A superseded run's command is deleted when the new one is sent.
        "stale_command_entry_id": previous_entry,
    }


# -- running a request ---------------------------------------------------------------


async def affected_guild_ids(session: AsyncSession) -> list[str]:
    """Every guild where the chat bot may hold memory or history."""
    guilds: set[str] = set()
    for column in (
        ChatAgentGuildMemory.guild_id,
        ChatAgentMemoryNote.guild_id,
        ChatAgentMemoryRevision.guild_id,
        ProactiveChannelSettings.guild_id,
        ProactiveAgentHistory.guild_id,
        ChatAgentEngagement.guild_id,
    ):
        guilds.update(g for g in (await session.scalars(select(column).distinct())).all() if g)
    return sorted(guilds)


async def _update_request(
    session_factory: SessionFactory,
    request_id: UUID,
    run_id: UUID,
    change: Callable[[ChatBotPurgeRequest], None],
) -> bool:
    """Apply ``change`` under a row lock if ``run_id`` is still the current run."""
    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
        if request is None or request.run_id != run_id or request.status == STATUS_CLOSED:
            return False
        change(request)
        _touch_json(request)
        await session.commit()
        return True


def _touch_json(request: ChatBotPurgeRequest) -> None:
    # A plain JSON column does not notice in-place mutation.
    flag_modified(request, "steps")


async def run_purge(
    request_id: UUID,
    run_id: UUID,
    *,
    session_factory: SessionFactory,
    redis,
    now: Callable[[], datetime],
    agent=None,
    wait_seconds: float = ENFORCING_WAIT_SECONDS,
    poll_seconds: float = ENFORCING_POLL_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> str:
    """Run one purge to the point where only the runtimes' acks are missing.

    Returns the status the request was left in. A run superseded by a newer
    submit stops at its next step without touching anything.
    """
    from smarter_dev.web.chat_memory_purge import purge_guild_memory

    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id)
        if request is None or request.run_id != run_id or request.discord_user_id is None:
            return "superseded"
        target = PurgeTarget.build(request.discord_user_id, request.names or [])
        list_revision = int(request.steps.get("list_revision", 0))
        stale_entry = request.steps.get("stale_command_entry_id")

    # 1. Both runtimes must be enforcing the list that blocks this user.
    waited = 0.0
    while not all_enforcing(await runtimes_enforcing(redis), list_revision):
        if waited >= wait_seconds:
            if stale_entry:
                await redis.xdel(PURGE_STREAM, stale_entry)
            await _update_request(
                session_factory,
                request_id,
                run_id,
                lambda r: setattr(r, "status", STATUS_WAITING),
            )
            logger.warning("Purge run %s: runtimes are not enforcing the block list", run_id)
            return STATUS_WAITING
        await sleep(poll_seconds)
        waited += poll_seconds

    if not await _update_request(
        session_factory, request_id, run_id, lambda r: setattr(r, "status", STATUS_PURGING)
    ):
        return "superseded"

    # 2. Guild memory, one transaction per guild.
    async with session_factory() as session:
        guild_ids = await affected_guild_ids(session)
    memory_steps: dict[str, dict] = {}
    for guild_id in guild_ids:
        async with session_factory() as session:
            try:
                result = await purge_guild_memory(
                    session, guild_id=guild_id, target=target, now=now(), agent=agent
                )
                await session.commit()
                memory_steps[guild_id] = result.as_step()
            except Exception as error:  # noqa: BLE001 — one guild must not stop the rest
                await session.rollback()
                # Type only: a validation error's text can quote memory.
                logger.error(
                    "Purge run %s: guild memory failed for guild %s (%s)",
                    run_id,
                    guild_id,
                    type(error).__name__,
                )
                memory_steps[guild_id] = {"outcome": "failed", "error": type(error).__name__}

    # 3. One command for the runtimes' working histories.
    entry_id = None
    if stale_entry:
        await redis.xdel(PURGE_STREAM, stale_entry)
    if guild_ids:
        command = PurgeCommand(
            schema_version=1,
            request_id=request_id,
            run_id=run_id,
            user_id=target.user_id,
            names=list(target.names),
            guild_ids=guild_ids,
            created_at=now(),
        )
        entry_id = await redis.xadd(PURGE_STREAM, {"payload": command.model_dump_json()})
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode()

    def record(request: ChatBotPurgeRequest) -> None:
        request.steps["guild_ids"] = guild_ids
        request.steps["memory"] = memory_steps
        request.steps["command_entry_id"] = entry_id
        request.steps["stale_command_entry_id"] = None
        # A fast runtime can acknowledge before this commit lands.
        done = acks_complete(request.steps)
        request.status = STATUS_CHECKING if done else STATUS_AWAITING_ACKS
        settled.append(request.status)

    settled: list[str] = []
    if not await _update_request(session_factory, request_id, run_id, record):
        if entry_id:
            await redis.xdel(PURGE_STREAM, entry_id)
        return "superseded"
    return settled[0]


def acks_complete(steps: dict) -> bool:
    guild_ids = steps.get("guild_ids") or []
    return all(
        guild_id in (steps.get(component) or {})
        for component in RUNTIME_COMPONENTS
        for guild_id in guild_ids
    )


async def record_ack(
    session: AsyncSession, run_id: UUID, ack: PurgeAck
) -> ChatBotPurgeRequest | None:
    """Store one runtime's result for one guild; the caller commits.

    Returns the request, or ``None`` for a run that is unknown or superseded
    (the runtime drops its command). A repeated ack replaces the earlier one.
    """
    request = await session.scalar(
        select(ChatBotPurgeRequest)
        .where(ChatBotPurgeRequest.run_id == run_id)
        .with_for_update()
    )
    if request is None:
        return None
    request.steps.setdefault(ack.component, {})[ack.guild_id] = {
        "outcome": ack.outcome,
        "stores": ack.stores,
        "detail": ack.detail,
    }
    if request.status == STATUS_AWAITING_ACKS and acks_complete(request.steps):
        request.status = STATUS_CHECKING
    _touch_json(request)
    await session.flush()
    return request


# -- the check ---------------------------------------------------------------------


# History and summary stores the purge rewrites: a hit here is a remain.
HISTORY_KEY_PATTERNS = (
    "chat_agent:*:history",
    "chat_agent:*:topic",
    "chat_agent:*:notes",
    "proactive:guild-history:*",
    "proactive:*:history",
)
# Raw operational copies the purge does not rewrite (#72/#75): reported only.
INFO_KEY_PATTERNS = (
    "proactive:v1:{guild:*}:wake",
    "proactive:v1:{guild:*}:pending",
    "proactive:v1:{guild:*}:batch:*",
    "proactive:v1:dead-letter",
    "proactive:v1:shadow",
    "chat_agent:guild:*:events",
)
STREAM_READ_LIMIT = 5000


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _redis_text(redis, key: str) -> str:
    kind = _decode(await redis.type(key))
    if kind == "string":
        return _decode(await redis.get(key) or b"")
    if kind == "list":
        return "\n".join(_decode(v) for v in await redis.lrange(key, 0, -1))
    if kind == "stream":
        entries = await redis.xrange(key, count=STREAM_READ_LIMIT)
        return "\n".join(
            _decode(v) for _, fields in entries for v in fields.values()
        )
    if kind == "hash":
        return "\n".join(_decode(v) for v in (await redis.hgetall(key)).values())
    if kind == "zset":
        return "\n".join(_decode(v) for v in await redis.zrange(key, 0, -1))
    if kind == "set":
        return "\n".join(_decode(v) for v in await redis.smembers(key))
    return ""


def _hit(store: str, location: str, target: PurgeTarget, text: str) -> dict | None:
    ids, names = target.id_hits(text), target.name_hits(text)
    if not ids and not names:
        return None
    return {"store": store, "location": location, "id_hits": ids, "name_hits": names}


async def scan_stores(session: AsyncSession, redis, target: PurgeTarget) -> dict:
    """Search every store the purge touched for the ID and the names.

    Returns locations and counts, never the text. ``remains`` are hits in
    stores the purge rewrites; ``operational`` are hits in raw copies it does
    not (queues, dead letters, event log), reported for the admin.
    """
    remains: list[dict] = []
    checked = 0

    for memory in (await session.scalars(select(ChatAgentGuildMemory))).all():
        checked += 1
        for field_name in ("content", "behavior", "personality"):
            hit = _hit(
                "chat_agent_guild_memory",
                f"guild:{memory.guild_id}/{field_name}",
                target,
                getattr(memory, field_name) or "",
            )
            if hit:
                remains.append(hit)
    for note in (await session.scalars(select(ChatAgentMemoryNote))).all():
        checked += 1
        hit = _hit(
            "chat_agent_memory_notes",
            f"guild:{note.guild_id}/note:{note.id}",
            target,
            note.content,
        )
        if hit:
            remains.append(hit)
    for revision in (await session.scalars(select(ChatAgentMemoryRevision))).all():
        checked += 1
        hit = _hit(
            "chat_agent_memory_revisions",
            f"guild:{revision.guild_id}/revision:{revision.revision}",
            target,
            f"{revision.content}\n{revision.behavior}\n{revision.personality}",
        )
        if hit:
            remains.append(hit)
    for settings in (await session.scalars(select(ProactiveChannelSettings))).all():
        checked += 1
        hit = _hit(
            "proactive_channel_settings.watch_addendum",
            f"guild:{settings.guild_id}/channel:{settings.channel_id}",
            target,
            settings.watch_addendum or "",
        )
        if hit:
            remains.append(hit)
    for history in (await session.scalars(select(ProactiveAgentHistory))).all():
        checked += 1
        hit = _hit(
            "proactive_agent_histories",
            f"guild:{history.guild_id}",
            target,
            json.dumps(history.history, ensure_ascii=False),
        )
        if hit:
            remains.append(hit)

    seen: set[str] = set()
    for pattern in HISTORY_KEY_PATTERNS:
        async for raw_key in redis.scan_iter(match=pattern, count=500):
            key = _decode(raw_key)
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            hit = _hit("redis", key, target, await _redis_text(redis, key))
            if hit:
                remains.append(hit)

    operational: list[dict] = []
    for pattern in INFO_KEY_PATTERNS:
        async for raw_key in redis.scan_iter(match=pattern, count=500):
            key = _decode(raw_key)
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            hit = _hit("redis", key, target, await _redis_text(redis, key))
            if hit:
                operational.append(hit)

    return {"stores_checked": checked, "remains": remains, "operational": operational}


def run_outcome(steps: dict, report: dict) -> str:
    """``complete`` only when every step succeeded and nothing remains to review."""
    memory = steps.get("memory") or {}
    failed = any(step.get("outcome") == "failed" for step in memory.values())
    unresolved = any(step.get("unresolved") for step in memory.values())
    for component in RUNTIME_COMPONENTS:
        acks = steps.get(component) or {}
        failed = failed or any(ack.get("outcome") == "failed" for ack in acks.values())
    if failed or unresolved or report.get("remains"):
        return STATUS_NEEDS_REVIEW
    return STATUS_COMPLETE


async def run_check(
    request_id: UUID,
    *,
    session_factory: SessionFactory,
    redis,
    now: Callable[[], datetime],
) -> str | None:
    """Search every store, store the report and settle the request's status."""
    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id)
        if request is None or request.discord_user_id is None:
            return None
        run_id = request.run_id
        target = PurgeTarget.build(request.discord_user_id, request.names or [])
        report = await scan_stores(session, redis, target)
    report["checked_at"] = now().isoformat()

    entry_to_delete: list[str] = []

    def settle(request: ChatBotPurgeRequest) -> None:
        request.check_report = report
        # Re-running the check by hand mid-run must not end a run still
        # waiting for acknowledgements.
        if request.status in (STATUS_CHECKING, STATUS_COMPLETE, STATUS_NEEDS_REVIEW):
            request.status = run_outcome(request.steps, report)
            request.completed_at = now()
        if acks_complete(request.steps) and request.steps.get("command_entry_id"):
            entry_to_delete.append(request.steps["command_entry_id"])
            request.steps["command_entry_id"] = None

    if not await _update_request(session_factory, request_id, run_id, settle):
        return None
    for entry_id in entry_to_delete:
        await redis.xdel(PURGE_STREAM, entry_id)
    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id)
        return request.status if request else None


# -- closing ----------------------------------------------------------------------


def receipt_summary(request: ChatBotPurgeRequest) -> dict:
    steps = request.steps or {}
    report = request.check_report or {}
    return {
        "guilds": len(steps.get("guild_ids") or []),
        "outcome": request.status,
        "remains": len(report.get("remains") or []),
        "operational": len(report.get("operational") or []),
    }


async def close_request(
    session: AsyncSession, request_id: UUID, *, now: datetime, redis=None
) -> ChatBotPurgeRequest | None:
    """Strip the request to a bare receipt; the caller commits.

    The ID, names, per-guild details and check locations go. What remains is
    when it was asked, when it finished, and counts. The block-list row is
    untouched: it is what keeps the person out of the chat bot from now on.
    """
    request = await session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
    if request is None:
        return None
    steps = request.steps or {}
    for key in ("command_entry_id", "stale_command_entry_id"):
        if steps.get(key) and redis is not None:
            await redis.xdel(PURGE_STREAM, steps[key])
    summary = receipt_summary(request)
    request.discord_user_id = None
    request.names = None
    request.steps = {"receipt": summary}
    request.check_report = None
    request.status = STATUS_CLOSED
    request.closed_at = now
    # A run still in flight fails its next write and deletes its own command.
    request.run_id = None
    await session.flush()
    return request
