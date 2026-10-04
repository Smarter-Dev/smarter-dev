"""One admin request to remove a Discord user from the chat bot, end to end.

The order is the point:

1. **Stop reading them.** Opening a request puts the user on the block list
   (:class:`~smarter_dev.web.models.ChatBotBlockedUser`). The run then waits
   until every live process of both runtimes reports enforcing that list
   revision, so no wake in between can read their old Discord messages back
   into history.
2. **Guild memory.** The agent rewrites each guild's blocks, notes and
   revisions without them (:mod:`smarter_dev.web.chat_memory_purge`). The
   model calls run outside any transaction; each guild's write is a short
   compare-and-set transaction.
3. **Working history.** One :class:`~smarter_dev.shared.privacy_purge.PurgeCommand`
   goes to the bot and the external worker, which each run a forced privacy
   compaction over the histories they hold and acknowledge per guild.
4. **A final notes pass.** The bot can write a note about the person from a
   history it had not folded yet, so once every guild is acknowledged the
   agent reviews the notes written since step 2 and every note that still
   mentions them.
5. **The check.** A deterministic search of every store touched reports where
   the ID or a name is still found. It does not take the agent's word for
   anything.

Every step is safe to repeat: a clean store comes back unchanged. A re-run
skips a guild whose memory this request already purged with the same names,
unless the last check, an unresolved item or a runtime's ack flagged it.
Closing a request strips the ID and names, leaving a bare receipt; the
block-list row stays, because it is what keeps the deletion true.

A run holds a lease on its request (``steps["lease"]``), and re-checks before
every guild and inside every guild's write that it is still the request's
current run and the request is not closed. A second execution of the same job
(the job queue can re-claim a run that outlives its visibility timeout) finds
the lease held and does nothing.

This module is imported by the web tier (admin page, bot API), so it must stay
light: the agent stack is imported only inside the run and the check.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import RUNTIME_COMPONENTS
from smarter_dev.shared.privacy_purge import SNOWFLAKE_PATTERN
from smarter_dev.shared.privacy_purge import BlockedUsers
from smarter_dev.shared.privacy_purge import PurgeAck
from smarter_dev.shared.privacy_purge import PurgeCommand
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.privacy_purge import ack_flags
from smarter_dev.shared.privacy_purge import consumer_pattern
from smarter_dev.shared.privacy_purge import enforcing_process_pattern
from smarter_dev.shared.privacy_purge import history_tombstone_key
from smarter_dev.web.models import ChatAgentCompactionEvent
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentError
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision
from smarter_dev.web.models import ChatAgentTurn
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
STATUS_FINISHING = "finishing"
STATUS_COMPLETE = "complete"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_CLOSED = "closed"
# A job raised and left the request mid-run; the page offers "Run the purge again".
STATUS_FAILED = "failed"

BLOCK_SOURCE_PURGE = "purge"
MAX_NAMES = 20
# How long a run waits for both runtimes to enforce the new block list.
ENFORCING_WAIT_SECONDS = 600
ENFORCING_POLL_SECONDS = 5
# A run's hold on its request. Renewed before every guild; longer than the
# enforcing wait and than one guild's model calls are expected to take.
LEASE_SECONDS = 1200
# Commands older than this are trimmed from the stream by every XADD
# (``MINID``, exact), so an entry that every delete path missed still goes.
COMMAND_RETENTION = timedelta(days=7)
STREAM_PAGE = 500
MAX_COMMAND_GUILDS = 500

# The stores that make up a guild's memory: a check hit here flags the guild.
MEMORY_STORES = frozenset(
    {"chat_agent_guild_memory", "chat_agent_memory_notes", "chat_agent_memory_revisions"}
)
SessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


def _insert(session: AsyncSession):
    return pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert


# -- the block list ---------------------------------------------------------------


async def block_list_revision(session: AsyncSession) -> int:
    row = await session.get(ChatBotBlockedUsersRevision, 1)
    return row.revision if row is not None else 0


async def add_blocked_user(
    session: AsyncSession, discord_user_id: str, *, source: str = BLOCK_SOURCE_PURGE
) -> int:
    """Put ``discord_user_id`` on the block list; return the list's revision.

    Adding someone already listed changes nothing and returns the current
    revision, which is what makes a repeated request safe. Both inserts are
    ``ON CONFLICT DO NOTHING`` and the revision is bumped by the database, so
    two admins blocking at once never collide.
    """
    insert = _insert(session)
    added = await session.execute(
        insert(ChatBotBlockedUser)
        .values(discord_user_id=discord_user_id, source=source)
        .on_conflict_do_nothing(index_elements=["discord_user_id"])
    )
    if not added.rowcount:
        return await block_list_revision(session)
    await session.execute(
        insert(ChatBotBlockedUsersRevision)
        .values(id=1, revision=0)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    revision = await session.scalar(
        update(ChatBotBlockedUsersRevision)
        .where(ChatBotBlockedUsersRevision.id == 1)
        .values(revision=ChatBotBlockedUsersRevision.revision + 1)
        .returning(ChatBotBlockedUsersRevision.revision)
    )
    return int(revision)


async def read_blocked_users(session: AsyncSession) -> BlockedUsers:
    # Revision first: if a block commits between the two reads, the list is
    # newer than its revision (safe) rather than older (a runtime would report
    # enforcing a revision whose user it is still reading).
    revision = await block_list_revision(session)
    user_ids = (await session.scalars(select(ChatBotBlockedUser.discord_user_id))).all()
    return BlockedUsers(revision=revision, user_ids=sorted(user_ids))


# -- the runtimes ------------------------------------------------------------------


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def runtime_status(redis) -> dict[str, dict]:
    """Per component: live processes, the MIN revision they enforce, live consumers.

    Computed from the per-process keys (``privacy:v1:enforcing:{c}:{host-pid}``
    and ``privacy:v1:consumer:{c}:{host-pid}``, each EX 180), never from the
    aggregate key a runtime publishes: a stale aggregate cannot outlive a
    lower process. A component with no live process key is not enforcing.
    A process that has never reported (cold start, or a build that predates
    per-process reporting) has no key and is not counted; its own
    fail-closed rule keeps Discord input from its models until it loads the
    list, but an old build never reports at all, which is why the page tells
    the admin to start a first purge only once every pod runs a privacy build.
    """
    # One SCAN for everything (L7); the per-component patterns are matched here.
    keys: list[str] = []
    async for raw_key in redis.scan_iter(match="privacy:v1:*", count=1000):
        key = _decode(raw_key)
        if key.startswith(("privacy:v1:enforcing:", "privacy:v1:consumer:")):
            keys.append(key)
    values = dict(zip(keys, await redis.mget(keys), strict=True)) if keys else {}
    status: dict[str, dict] = {}
    for component in RUNTIME_COMPONENTS:
        enforcing_prefix = enforcing_process_pattern(component)[:-1]
        consumer_prefix = consumer_pattern(component)[:-1]
        revisions = []
        consumers = 0
        for key, raw in values.items():
            if raw is None:
                continue
            if key.startswith(enforcing_prefix):
                try:
                    revisions.append(int(_decode(raw)))
                except ValueError:
                    continue
            elif key.startswith(consumer_prefix):
                consumers += 1
        status[component] = {
            "processes": len(revisions),
            "revision": min(revisions) if revisions else None,
            "consumers": consumers,
        }
    return status


async def runtimes_enforcing(redis) -> dict[str, int | None]:
    """The block-list revision each runtime enforces: the MIN over its live processes."""
    return {c: s["revision"] for c, s in (await runtime_status(redis)).items()}


def all_enforcing(enforcing: dict[str, int | None], revision: int = 0) -> bool:
    return all(value is not None and value >= revision for value in enforcing.values())


def start_refusal(status: dict[str, dict]) -> str | None:
    """Why a purge may not start now, as a sentence for the admin, or ``None``."""
    not_enforcing = [c for c, s in status.items() if s["revision"] is None]
    if not_enforcing:
        return (
            f"Not enforcing the block list yet: {', '.join(not_enforcing)}. A purge started "
            "now could be undone by the next wake, so it is not started. Deploy or restart "
            "that runtime, wait a minute and try again."
        )
    no_consumer = [c for c, s in status.items() if not s["consumers"]]
    if no_consumer:
        return (
            f"No live purge consumer: {', '.join(no_consumer)}. Nothing would purge that "
            "runtime's history, so the purge is not started. Deploy or restart it, wait a "
            "minute and try again."
        )
    return None


# -- opening a request --------------------------------------------------------------


def clean_names(names: list[str]) -> list[str]:
    target = PurgeTarget.build("0", [n[:100] for n in names])
    return list(target.names[:MAX_NAMES])


async def _open_request_for(session: AsyncSession, discord_user_id: str):
    return await session.scalar(
        select(ChatBotPurgeRequest)
        .where(
            ChatBotPurgeRequest.discord_user_id == discord_user_id,
            ChatBotPurgeRequest.status != STATUS_CLOSED,
        )
        .with_for_update()
    )


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
    A partial unique index allows one open request per user; losing the race
    to insert it reuses the winner's row.
    """
    revision = await add_blocked_user(session, discord_user_id)
    request = await _open_request_for(session, discord_user_id)
    if request is None:
        request = ChatBotPurgeRequest(
            discord_user_id=discord_user_id,
            requested_by=requested_by,
            names=[],
            status=STATUS_QUEUED,
            steps={},
        )
        try:
            async with session.begin_nested():
                session.add(request)
        except IntegrityError:
            request = await _open_request_for(session, discord_user_id)
            if request is None:
                raise
    request.names = clean_names([*(request.names or []), *names])
    start_new_run(request, list_revision=revision)
    await session.flush()
    return request


async def remove_unchecked_name(
    session: AsyncSession, request_id: UUID, name: str, *, now: datetime
) -> str | None:
    """Drop one never-searched name from a request; the caller commits.

    Only a name the matcher reports as unchecked (ASCII, under 2 characters)
    can go, so this can never weaken what a purge searches for, and only
    while no run holds the request. Returns a refusal sentence, or None.
    A later submit merges the name back only if the admin enters it again.
    """
    request = await session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
    if request is None or request.discord_user_id is None or request.status == STATUS_CLOSED:
        return "A closed request has no names to remove."
    if request.status in (STATUS_PURGING, STATUS_FINISHING) or not _lease_free(
        request.steps or {}, "", now
    ):
        return "A run is working on this request; remove the name once it has finished."
    target = PurgeTarget.build(request.discord_user_id, request.names or [])
    if name not in target.unchecked_names:
        return "Only a name that was never searched (one ASCII character) can be removed."
    request.names = [n for n in (request.names or []) if n != name]
    await session.flush()
    return None


def ack_flagged(ack: dict) -> bool:
    """From the ack's structured fields only (see :func:`ack_flags`)."""
    return bool(ack_flags(ack))


def possible_remains(steps: dict) -> list[dict]:
    """Every flagged ack, with its reasons: name hits, a tombstone, unchecked
    names, missing structured fields, or a failure."""
    found = []
    for component in RUNTIME_COMPONENTS:
        for guild_id, ack in sorted((steps.get(component) or {}).items()):
            reasons = ack_flags(ack)
            if reasons:
                found.append({"guild_id": guild_id, "component": component, "reasons": reasons})
    return found


def flagged_guilds(steps: dict, report: dict | None) -> set[str]:
    """Guilds a re-run must purge again even if this request already did.

    The last check found the ID or a name in the guild's memory stores, or a
    memory step (or the final notes pass) left something unresolved, or a
    runtime's ack still counted name hits or reported a tombstone, or the
    check found the guild's history tombstoned.
    """
    flagged = {
        hit["guild_id"]
        for hit in (report or {}).get("remains") or []
        if hit.get("store") in MEMORY_STORES and hit.get("guild_id")
    }
    for section in ("memory", "final_notes"):
        for guild_id, step in (steps.get(section) or {}).items():
            if step.get("unresolved"):
                flagged.add(guild_id)
    flagged.update(item["guild_id"] for item in possible_remains(steps))
    flagged.update((report or {}).get("tombstoned") or [])
    return flagged


def start_new_run(request: ChatBotPurgeRequest, *, list_revision: int) -> None:
    steps = request.steps or {}
    flagged = flagged_guilds(steps, request.check_report)
    memory_done = {
        guild_id: done
        for guild_id, done in (steps.get("memory_done") or {}).items()
        if guild_id not in flagged
    }
    request.run_id = uuid4()
    request.status = STATUS_QUEUED
    request.completed_at = None
    request.check_report = None
    request.steps = {
        "list_revision": list_revision,
        "guild_ids": [],
        "memory": {},
        "final_notes": {},
        "bot": {},
        "worker": {},
        # Guilds whose memory this request already purged, and with which
        # names: a re-run skips them rather than have the agent rewrite other
        # members' memory again, unless a name was added since or the guild
        # was flagged (see :func:`flagged_guilds`).
        "memory_done": memory_done,
    }


def names_key(target: PurgeTarget) -> str:
    """A digest of the name list: ``steps`` must not hold the names themselves
    (a failed UPDATE quotes its parameters in the error text)."""
    joined = "\n".join(sorted(name.casefold() for name in target.names))
    return hashlib.sha256(joined.encode()).hexdigest()


# -- the command stream ------------------------------------------------------------


async def _stream_entries(redis, key: str) -> AsyncIterator[tuple[str, dict]]:
    """Every entry of a stream, in pages, oldest first."""
    start = "-"
    while True:
        page = await redis.xrange(key, min=start, count=STREAM_PAGE)
        if not page:
            return
        for entry_id, fields in page:
            yield _decode(entry_id), fields
        if len(page) < STREAM_PAGE:
            return
        start = f"({_decode(page[-1][0])}"


def _payload(fields: dict) -> dict:
    raw = fields.get(b"payload", fields.get("payload"))
    if raw is None:
        return {}
    try:
        value = json.loads(_decode(raw))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def delete_commands(
    redis,
    *,
    request_id: UUID | None = None,
    run_id: UUID | None = None,
    except_run: UUID | None = None,
) -> int:
    """XDEL every purge command of ``request_id`` (but ``except_run``'s) or of ``run_id``.

    Found by reading the stream, not by a remembered entry ID, so a command
    whose ID was lost (the job died between XADD and recording it) is found
    too. The stream is small: entries are deleted once acknowledged and
    trimmed by age on every XADD.
    """
    doomed = []
    async for entry_id, fields in _stream_entries(redis, PURGE_STREAM):
        payload = _payload(fields)
        entry_request, entry_run = payload.get("request_id"), payload.get("run_id")
        if request_id is not None and entry_request == str(request_id):
            if except_run is None or entry_run != str(except_run):
                doomed.append(entry_id)
        elif run_id is not None and entry_run == str(run_id):
            doomed.append(entry_id)
    if doomed:
        await redis.xdel(PURGE_STREAM, *doomed)
    return len(doomed)


async def find_command(redis, run_id: UUID) -> str | None:
    """The stream entry ID of ``run_id``'s command, if it is still there."""
    async for entry_id, fields in _stream_entries(redis, PURGE_STREAM):
        if _payload(fields).get("run_id") == str(run_id):
            return entry_id
    return None


def _cutoff_id(now: datetime) -> str:
    cutoff_ms = int((now - COMMAND_RETENTION).timestamp() * 1000)
    return f"{max(cutoff_ms, 0)}-0"


async def trim_commands(redis, *, now: datetime) -> None:
    """Drop commands older than :data:`COMMAND_RETENTION` (exact MINID).

    Run on every XADD, on every enforcing poll, and whenever an admin page
    reads the runtimes, so the bound holds even when no purge is sent.
    """
    try:
        await redis.xtrim(PURGE_STREAM, minid=_cutoff_id(now), approximate=False)
    except Exception as error:  # noqa: BLE001 — trimming is best effort, retried next time
        logger.warning("Purge stream trim failed (%s)", type(error).__name__)


async def delete_commands_retrying(
    redis, *, attempts: int = 3, delay: float = 0.5, sleep=asyncio.sleep, **match
) -> bool:
    """:func:`delete_commands`, retried; False if every attempt failed."""
    for attempt in range(attempts):
        try:
            await delete_commands(redis, **match)
            return True
        except Exception as error:  # noqa: BLE001 — retried, then reported
            logger.warning(
                "Purge command delete failed, attempt %d (%s)", attempt + 1, type(error).__name__
            )
            if attempt < attempts - 1:
                await sleep(delay * 2**attempt)
    return False


def command_problem(guild_ids: list[str]) -> str | None:
    """Why no valid command can carry ``guild_ids``, in the admin's words."""
    bad = [g for g in guild_ids if not re.fullmatch(SNOWFLAKE_PATTERN, g)]
    if bad:
        shown = ", ".join(repr(g[:30]) for g in bad[:5])
        return (
            f"{len(bad)} stored guild ID(s) are not Discord snowflakes (15 to 22 digits): "
            f"{shown}. Fix or remove those rows, then run the purge again."
        )
    if len(guild_ids) > MAX_COMMAND_GUILDS:
        return (
            f"The bot holds memory or history for {len(guild_ids)} guilds, more than the "
            f"{MAX_COMMAND_GUILDS} one purge command can carry."
        )
    return None


async def send_command(redis, command: PurgeCommand, *, now: datetime) -> str:
    """XADD the command, trimming entries older than :data:`COMMAND_RETENTION`."""
    entry_id = await redis.xadd(
        PURGE_STREAM,
        {"payload": command.model_dump_json()},
        minid=_cutoff_id(now),
        approximate=False,
    )
    return _decode(entry_id)


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


def _is_current(request: ChatBotPurgeRequest | None, run_id: UUID) -> bool:
    return request is not None and request.run_id == run_id and request.status != STATUS_CLOSED


async def _update_request(
    session_factory: SessionFactory,
    request_id: UUID,
    run_id: UUID,
    change: Callable[[ChatBotPurgeRequest], bool | None],
) -> bool:
    """Apply ``change`` under a row lock if ``run_id`` is still the current run.

    ``change`` returning ``False`` means "not mine after all": nothing is written.
    """
    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
        if not _is_current(request, run_id):
            await session.rollback()
            return False
        if change(request) is False:
            await session.rollback()
            return False
        _touch_json(request)
        await session.commit()
        return True


def _touch_json(request: ChatBotPurgeRequest) -> None:
    # A plain JSON column does not notice in-place mutation.
    flag_modified(request, "steps")


def _lease_free(steps: dict, token: str, now: datetime) -> bool:
    lease = steps.get("lease") or {}
    if not lease or lease.get("token") == token:
        return True
    try:
        until = datetime.fromisoformat(lease["until"])
    except (KeyError, TypeError, ValueError):
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until <= now


def _take_lease(token: str, now: datetime) -> Callable[[ChatBotPurgeRequest], bool]:
    def change(request: ChatBotPurgeRequest) -> bool:
        if not _lease_free(request.steps, token, now):
            return False
        request.steps["lease"] = {
            "token": token,
            "until": (now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
        }
        return True

    return change


def _holds(steps: dict, token: str) -> bool:
    return (steps.get("lease") or {}).get("token") == token


def _still_current(request_id: UUID, run_id: UUID, token: str):
    """Checked inside each guild's write transaction, holding the request row."""

    async def check(session: AsyncSession) -> bool:
        request = await session.get(
            ChatBotPurgeRequest, request_id, with_for_update=True, populate_existing=True
        )
        return _is_current(request, run_id) and _holds(request.steps, token)

    return check


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
    submit, or whose request was closed, stops at its next check without
    writing and deletes its own command if it sent one. A second execution of
    a run that is still going returns ``"already_running"``.
    """
    from smarter_dev.web.chat_memory_purge import PurgeStopped
    from smarter_dev.web.chat_memory_purge import purge_guild_memory

    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id)
        if not _is_current(request, run_id) or request.discord_user_id is None:
            return "superseded"
        target = PurgeTarget.build(request.discord_user_id, request.names or [])
        list_revision = int(request.steps.get("list_revision", 0))
        memory_done = dict(request.steps.get("memory_done") or {})
        intent = request.steps.get("command_intent")

    token = str(uuid4())
    take = _take_lease(token, now())

    def start(request: ChatBotPurgeRequest) -> bool:
        # A run that already sent its command is past this job: a second
        # execution of the same job (re-claimed by the queue) must not redo it.
        if request.status not in (STATUS_QUEUED, STATUS_WAITING, STATUS_PURGING):
            return False
        return take(request)

    if not await _update_request(session_factory, request_id, run_id, start):
        async with session_factory() as session:
            request = await session.get(ChatBotPurgeRequest, request_id)
            current = _is_current(request, run_id)
        return "already_running" if current else "superseded"

    async def superseded() -> str:
        await delete_commands(redis, run_id=run_id)
        return "superseded"

    def mine(change: Callable[[ChatBotPurgeRequest], Any]):
        def guarded(request: ChatBotPurgeRequest):
            if not _holds(request.steps, token):
                return False
            return change(request)

        return guarded

    # 1. Both runtimes must be enforcing the list that blocks this user.
    # A runtime that has loaded the list once keeps running on its last list
    # through a list outage and keeps reporting the (possibly stale) revision
    # it holds, so the MIN over live processes is exactly what to wait on.
    waited = 0.0
    while not all_enforcing(enforcing := await runtimes_enforcing(redis), list_revision):
        if waited >= wait_seconds:
            behind = ", ".join(
                f"{component} holds {'no list' if value is None else f'revision {value}'}"
                for component, value in enforcing.items()
                if value is None or value < list_revision
            )
            reason = (
                f"Nothing was purged: the runtimes did not enforce block-list revision "
                f"{list_revision} within {int(wait_seconds)} s ({behind}). Run the purge again "
                "once every process has caught up."
            )

            def timed_out(r: ChatBotPurgeRequest, reason: str = reason) -> None:
                r.status = STATUS_NEEDS_REVIEW
                r.steps["error"] = reason
                r.steps.pop("lease", None)

            await _update_request(session_factory, request_id, run_id, mine(timed_out))
            logger.warning("Purge run %s: runtimes are not enforcing the block list", run_id)
            return STATUS_NEEDS_REVIEW
        if waited == 0:
            await _update_request(
                session_factory,
                request_id,
                run_id,
                mine(lambda r: setattr(r, "status", STATUS_WAITING)),
            )
        await trim_commands(redis, now=now())
        await sleep(poll_seconds)
        waited += poll_seconds

    # The command is built and validated before any memory is touched, so a
    # bad guild ID cannot fail the run after memory was rewritten.
    async with session_factory() as session:
        guild_ids = await affected_guild_ids(session)
    command = None
    problem = command_problem(guild_ids)
    if problem is None and guild_ids:
        try:
            command = PurgeCommand(
                schema_version=1,
                request_id=request_id,
                run_id=run_id,
                user_id=target.user_id,
                names=list(target.names),
                guild_ids=guild_ids,
                created_at=now(),
            )
        except ValidationError as error:
            # Field locations and messages only: the inputs are the ID and names.
            problem = "The purge command failed validation: " + "; ".join(
                f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
                for item in error.errors(include_input=False, include_url=False)[:5]
            )
    if problem is not None:
        logger.error("Purge run %s: the purge command cannot be built", run_id)

        def invalid(r: ChatBotPurgeRequest) -> None:
            r.status = STATUS_NEEDS_REVIEW
            r.steps["error"] = problem

        await _update_request(session_factory, request_id, run_id, mine(invalid))
        return STATUS_NEEDS_REVIEW

    memory_started_at = now()

    def purging(request: ChatBotPurgeRequest) -> None:
        request.status = STATUS_PURGING
        request.steps["guild_ids"] = guild_ids
        request.steps["memory_started_at"] = memory_started_at.isoformat()

    if not await _update_request(session_factory, request_id, run_id, mine(purging)):
        return await superseded()

    # 2. Guild memory, one compare-and-set write per guild.
    current_names = names_key(target)
    still_current = _still_current(request_id, run_id, token)
    for guild_id in guild_ids:
        # Before each guild: still this run, not closed, lease renewed.
        if not await _update_request(
            session_factory, request_id, run_id, _take_lease(token, now())
        ):
            return await superseded()
        done = memory_done.get(guild_id)
        step_started = now().isoformat()
        if done and done.get("names") == current_names:
            step = {**done["step"], "earlier_run": True}
            new_done = None
        else:
            try:
                result = await purge_guild_memory(
                    session_factory,
                    guild_id=guild_id,
                    target=target,
                    now=now(),
                    agent=agent,
                    still_current=still_current,
                )
                step = result.as_step()
                # The original time is kept across re-runs: the final notes
                # pass reviews every note written since the earliest one.
                new_done = {"names": current_names, "step": step, "at": step_started}
            except PurgeStopped:
                return await superseded()
            except Exception as error:  # noqa: BLE001 — one guild must not stop the rest
                # Type only: a validation or database error's text can quote
                # memory, the ID or a name.
                logger.error(
                    "Purge run %s: guild memory failed for guild %s (%s)",
                    run_id,
                    guild_id,
                    type(error).__name__,
                )
                step = {"outcome": "failed", "error": type(error).__name__}
                new_done = None

        def record_guild(r: ChatBotPurgeRequest, guild_id=guild_id, step=step, new_done=new_done):
            r.steps.setdefault("memory", {})[guild_id] = step
            if new_done is not None:
                r.steps.setdefault("memory_done", {})[guild_id] = new_done

        if not await _update_request(session_factory, request_id, run_id, mine(record_guild)):
            return await superseded()

    # 3. One command for the runtimes' working histories. The intent is
    # recorded first; every path that ends a run finds the entry by reading
    # the stream for this request or run, so a lost entry ID orphans nothing.
    await delete_commands(redis, request_id=request_id, except_run=run_id)
    entry_id = None
    if command is not None:
        if not await _update_request(
            session_factory,
            request_id,
            run_id,
            mine(lambda r: r.steps.__setitem__("command_intent", str(run_id))),
        ):
            return await superseded()
        # A second execution of this run (the first died after XADD) finds
        # the entry the first one sent instead of sending a second.
        entry_id = await find_command(redis, run_id) if intent == str(run_id) else None
        if entry_id is None:
            entry_id = await send_command(redis, command, now=now())

    settled: list[str] = []

    def record(request: ChatBotPurgeRequest) -> None:
        request.steps["command_entry_id"] = entry_id
        request.steps["command_sent_at"] = now().isoformat()
        request.steps.pop("lease", None)
        # A fast runtime can acknowledge before this commit lands.
        request.status = STATUS_CHECKING if acks_complete(request.steps) else STATUS_AWAITING_ACKS
        settled.append(request.status)

    if not await _update_request(session_factory, request_id, run_id, mine(record)):
        return await superseded()
    return settled[0]


def missing_acks(steps: dict) -> list[tuple[str, str]]:
    """(component, guild) pairs with no real ack; a timeout's stand-in does not count."""
    return [
        (component, guild_id)
        for component in RUNTIME_COMPONENTS
        for guild_id in steps.get("guild_ids") or []
        if not (ack := (steps.get(component) or {}).get(guild_id)) or ack.get("synthetic")
    ]


def acks_complete(steps: dict) -> bool:
    return not missing_acks(steps)


ACK_TIMEOUT = timedelta(hours=1)
ACK_TIMEOUT_DETAIL = "no ack within 1 hour"


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ack_timeout(now: datetime) -> Callable[[ChatBotPurgeRequest], bool]:
    def change(request: ChatBotPurgeRequest) -> bool:
        steps = request.steps
        if request.status != STATUS_AWAITING_ACKS:
            return False
        marks = [t for t in (steps.get("command_sent_at"), steps.get("last_ack_at")) if t]
        if not marks or now - max(_aware(t) for t in marks) <= ACK_TIMEOUT:
            return False
        missing = missing_acks(steps)
        for component, guild_id in missing:
            steps.setdefault(component, {})[guild_id] = {
                "outcome": "failed",
                "stores": [],
                "detail": ACK_TIMEOUT_DETAIL,
                "synthetic": True,
            }
        steps["ack_timeout"] = {"at": now.isoformat(), "missing": [list(m) for m in missing]}
        request.status = STATUS_NEEDS_REVIEW
        request.completed_at = now
        return True

    return change


async def expire_acks(session: AsyncSession, request_id: UUID, *, now: datetime) -> bool:
    """Move an ``awaiting_acks`` request with no progress for an hour to review.

    Progress is the command being sent or the latest ack. Every missing
    (component, guild) ack is recorded as failed ("no ack within 1 hour"),
    marked ``synthetic`` so a late real ack replaces it, and the command
    stays on the stream for the runtimes. Evaluated lazily, on every page
    view and check. Commits when it changes something.
    """
    request = await session.get(
        ChatBotPurgeRequest, request_id, with_for_update=True, populate_existing=True
    )
    if request is None or not _ack_timeout(now)(request):
        await session.rollback()
        return False
    _touch_json(request)
    await session.commit()
    return True


async def record_ack(
    session: AsyncSession, run_id: UUID, ack: PurgeAck, *, now: datetime | None = None
) -> ChatBotPurgeRequest | None:
    """Store one runtime's result for one guild; the caller commits.

    Returns the request, or ``None`` for a run that is unknown or superseded
    (the runtime drops its command, and the API deletes it from the stream).
    A repeated ack replaces the earlier one.
    """
    request = await session.scalar(
        select(ChatBotPurgeRequest)
        .where(ChatBotPurgeRequest.run_id == run_id)
        .with_for_update()
    )
    if request is None or request.status == STATUS_CLOSED:
        return None
    request.steps.setdefault(ack.component, {})[ack.guild_id] = ack.model_dump(
        exclude={"component", "guild_id"}
    )
    request.steps["last_ack_at"] = (now or datetime.now(UTC)).isoformat()
    if request.status == STATUS_AWAITING_ACKS and acks_complete(request.steps):
        request.status = STATUS_CHECKING
    elif (
        request.status == STATUS_NEEDS_REVIEW
        and request.steps.get("ack_timeout")
        and acks_complete(request.steps)
    ):
        # The late acks replaced every stand-in: the run goes on to its check.
        request.steps.pop("ack_timeout")
        request.status = STATUS_CHECKING
    elif request.status == STATUS_COMPLETE and ack.flags():
        # A late failed or flagged ack reopens a finished request for review.
        request.status = STATUS_NEEDS_REVIEW
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
    "proactive:v1:{guild:*}:pending-dropped",
    "proactive:v1:{guild:*}:batch:*",
    "proactive:v1:dead-letter",
    "proactive:v1:shadow",
    "proactive:v1:control",
    "proactive:v1:control-processed*",
    "chat_agent:guild:*:events",
)
# Tables the #71 runbook deletes from; the check only reports them.
INFO_TABLES = (
    (ChatAgentTurn, ("triggering_messages", "agent_output", "model_messages_delta")),
    (
        ChatAgentEngagement,
        ("activation_user_id", "activation_username", "last_topic", "last_notes"),
    ),
    (ChatAgentCompactionEvent, ("original_content", "summary")),
    (ChatAgentError, ("error_message", "traceback", "provider_body", "error_context")),
)
TABLE_PAGE = 500

# What the check does not search, shown on the request page.
OUTSIDE_THE_CHECK = (
    "Discord itself (messages, nicknames, the member list).",
    "Logs and traces: pod logs, Logfire, the job queue's records.",
    "Database backups, snapshots and Redis persistence files.",
    "What model providers retain from earlier calls.",
    "Redis keys outside the listed patterns and tables not listed here, "
    "such as bytes transactions and squad records.",
    "A runtime's in-process memory before it reloads from the stores.",
)


def _hit(store: str, location: str, ids: int, names: int, guild_id: str | None = None):
    if not ids and not names:
        return None
    hit = {"store": store, "location": location, "id_hits": ids, "name_hits": names}
    if guild_id:
        hit["guild_id"] = guild_id
    return hit


async def _redis_hits(redis, key: str, target: PurgeTarget) -> tuple[int, int]:
    """(id hits, name hits) in one key, every value JSON-decoded where it is JSON."""
    kind = _decode(await redis.type(key))
    values: list = []
    if kind == "string":
        values = [await redis.get(key) or b""]
    elif kind == "list":
        values = await redis.lrange(key, 0, -1)
    elif kind == "hash":
        for field_name, value in (await redis.hgetall(key)).items():
            values.extend((field_name, value))
    elif kind == "zset":
        values = await redis.zrange(key, 0, -1)
    elif kind == "set":
        values = list(await redis.smembers(key))
    elif kind == "stream":
        async for _entry_id, fields in _stream_entries(redis, key):
            values.extend(fields.values())
    ids = names = 0
    for value in values:
        i, n = target.stored_hits(value)
        ids += i
        names += n
    return ids, names


async def _table_hits(session: AsyncSession, model, columns, target: PurgeTarget):
    """Hits per row of one table, read in keyset pages."""
    store = model.__tablename__
    last = None
    while True:
        query = select(model.id, *(getattr(model, c) for c in columns)).order_by(model.id)
        if last is not None:
            query = query.where(model.id > last)
        rows = (await session.execute(query.limit(TABLE_PAGE))).all()
        for row in rows:
            ids, names = target.value_hits([v for v in row[1:] if v is not None])
            hit = _hit(store, f"{store}:{row[0]}", ids, names)
            if hit:
                yield hit
        if len(rows) < TABLE_PAGE:
            return
        last = rows[-1][0]


async def scan_stores(session: AsyncSession, redis, target: PurgeTarget) -> dict:
    """Search every store the purge touched for the ID and the names.

    Returns locations and counts, never the text. ``remains`` are hits in
    stores the purge rewrites; ``operational`` are hits in raw Redis copies it
    does not rewrite (queues, dead letters, event log); ``information`` are
    hits in the chat audit tables the #71 runbook deletes from. JSON values
    are decoded and every string searched, so an escape character never hides
    a name.
    """
    remains: list[dict] = []
    checked = 0

    for memory in (await session.scalars(select(ChatAgentGuildMemory))).all():
        checked += 1
        for field_name in ("content", "behavior", "personality"):
            text = getattr(memory, field_name) or ""
            hit = _hit(
                "chat_agent_guild_memory",
                f"guild:{memory.guild_id}/{field_name}",
                *target.value_hits(text),
                memory.guild_id,
            )
            if hit:
                remains.append(hit)
    for note in (await session.scalars(select(ChatAgentMemoryNote))).all():
        checked += 1
        hit = _hit(
            "chat_agent_memory_notes",
            f"guild:{note.guild_id}/note:{note.id}",
            *target.value_hits(note.content),
            note.guild_id,
        )
        if hit:
            remains.append(hit)
    for revision in (await session.scalars(select(ChatAgentMemoryRevision))).all():
        checked += 1
        ids, names = target.value_hits(
            [revision.content or "", revision.behavior or "", revision.personality or ""]
        )
        hit = _hit(
            "chat_agent_memory_revisions",
            f"guild:{revision.guild_id}/revision:{revision.revision}",
            ids,
            names,
            revision.guild_id,
        )
        if hit:
            remains.append(hit)
    for settings in (await session.scalars(select(ProactiveChannelSettings))).all():
        checked += 1
        text = settings.watch_addendum or ""
        hit = _hit(
            "proactive_channel_settings.watch_addendum",
            f"guild:{settings.guild_id}/channel:{settings.channel_id}",
            *target.value_hits(text),
            settings.guild_id,
        )
        if hit:
            remains.append(hit)
    for history in (await session.scalars(select(ProactiveAgentHistory))).all():
        checked += 1
        ids, names = target.value_hits(history.history)
        hit = _hit("proactive_agent_histories", f"guild:{history.guild_id}", ids, names, history.guild_id)
        if hit:
            remains.append(hit)

    seen: set[str] = set()

    async def scan(patterns, into: list[dict]) -> int:
        count = 0
        for pattern in patterns:
            async for raw_key in redis.scan_iter(match=pattern, count=500):
                key = _decode(raw_key)
                if key in seen:
                    continue
                seen.add(key)
                count += 1
                hit = _hit("redis", key, *await _redis_hits(redis, key, target))
                if hit:
                    into.append(hit)
        return count

    checked += await scan(HISTORY_KEY_PATTERNS, remains)
    operational: list[dict] = []
    checked += await scan(INFO_KEY_PATTERNS, operational)

    information: list[dict] = []
    for model, columns in INFO_TABLES:
        checked += 1
        async for hit in _table_hits(session, model, columns, target):
            information.append(hit)

    return {
        "stores_checked": checked,
        "remains": remains,
        "operational": operational,
        "information": information,
    }


def hit_counts(report: dict) -> dict[str, int]:
    """Hits per store over remains, operational and information: names and counts only."""
    counts: dict[str, int] = {}
    for section in ("remains", "operational", "information"):
        for hit in report.get(section) or []:
            counts[hit["store"]] = counts.get(hit["store"], 0) + 1
    return counts


def run_outcome(steps: dict, report: dict) -> str:
    """``complete`` only when every step succeeded and no searched store hits.

    Any hit (remains, operational copies, audit tables), a tombstoned guild,
    an unchecked name, or a flagged ack ends in review.
    """
    failed = bool(steps.get("error"))
    unresolved = False
    for section in ("memory", "final_notes"):
        for step in (steps.get(section) or {}).values():
            failed = failed or step.get("outcome") == "failed"
            unresolved = unresolved or bool(step.get("unresolved"))
    for component in RUNTIME_COMPONENTS:
        acks = steps.get(component) or {}
        failed = failed or any(ack_flagged(ack) for ack in acks.values())
    if (
        failed
        or unresolved
        or hit_counts(report)
        or report.get("tombstoned")
        or report.get("unchecked_names")
        or possible_remains(steps)
    ):
        return STATUS_NEEDS_REVIEW
    return STATUS_COMPLETE


async def tombstoned_guilds(redis, request_id: UUID, guild_ids: list[str]) -> list[str]:
    """Guilds whose v1 history the worker tombstoned for this request (or an unknown one)."""
    if not guild_ids:
        return []
    values = await redis.mget([history_tombstone_key(g) for g in guild_ids])
    found = []
    for guild_id, raw in zip(guild_ids, values, strict=True):
        if raw is None:
            continue
        try:
            value = json.loads(_decode(raw))
        except ValueError:
            value = None
        owner = value.get("request_id") if isinstance(value, dict) else None
        # A plain (older) value names no run: it may be this request's.
        if owner is None or owner == str(request_id):
            found.append(guild_id)
    return found


class CloseRefused(Exception):
    """A guild of the request is tombstoned; closing would drop its command."""

    def __init__(self, guild_ids: list[str]):
        super().__init__(f"{len(guild_ids)} tombstoned guild(s)")
        self.guild_ids = guild_ids


async def mark_failed(
    session_factory: SessionFactory,
    request_id: UUID,
    run_id: UUID | None,
    error_type: str,
) -> bool:
    """Leave a request whose job raised in ``failed`` (re-run allowed), type only.

    ``run_id`` None means the request's current run (the check job knows no run).
    """
    if run_id is None:
        async with session_factory() as session:
            request = await session.get(ChatBotPurgeRequest, request_id)
            if request is None or request.run_id is None:
                return False
            run_id = request.run_id

    def change(request: ChatBotPurgeRequest) -> None:
        request.status = STATUS_FAILED
        request.steps["error"] = f"The purge job stopped with {error_type}. Run the purge again."
        request.steps.pop("lease", None)

    return await _update_request(session_factory, request_id, run_id, change)


def _claim_finishing(token: str, now: datetime) -> Callable[[ChatBotPurgeRequest], bool]:
    """Only one check job runs the final notes pass; a dead one's lease expires."""
    take = _take_lease(token, now)

    def change(request: ChatBotPurgeRequest) -> bool:
        if request.status == STATUS_CHECKING or (
            request.status == STATUS_FINISHING and _lease_free(request.steps, token, now)
        ):
            if not take(request):
                return False
            request.status = STATUS_FINISHING
            return True
        return False

    return change


def notes_since(steps: dict) -> datetime | None:
    """The earliest memory step of any guild in this request, this run or an earlier one."""
    times = [steps.get("memory_started_at")]
    guilds = set(steps.get("guild_ids") or [])
    times += [
        done.get("at") for g, done in (steps.get("memory_done") or {}).items() if g in guilds
    ]
    parsed = [datetime.fromisoformat(t) for t in times if t]
    return min(parsed) if parsed else None


async def _final_notes_pass(
    request_id: UUID,
    run_id: UUID,
    token: str,
    target: PurgeTarget,
    steps: dict,
    *,
    session_factory: SessionFactory,
    now: Callable[[], datetime],
    agent=None,
) -> bool:
    """Review new and still-mentioning notes in every guild; False if stopped."""
    from smarter_dev.web.chat_memory_purge import PurgeStopped
    from smarter_dev.web.chat_memory_purge import purge_guild_notes

    since = notes_since(steps)
    still_current = _still_current(request_id, run_id, token)
    for guild_id in steps.get("guild_ids") or []:
        if not await _update_request(
            session_factory, request_id, run_id, _take_lease(token, now())
        ):
            return False
        try:
            result = await purge_guild_notes(
                session_factory,
                guild_id=guild_id,
                target=target,
                now=now(),
                since=since,
                agent=agent,
                still_current=still_current,
            )
            step = result.as_step()
        except PurgeStopped:
            return False
        except Exception as error:  # noqa: BLE001 — one guild must not stop the rest
            logger.error(
                "Purge run %s: final notes pass failed for guild %s (%s)",
                run_id,
                guild_id,
                type(error).__name__,
            )
            step = {"outcome": "failed", "error": type(error).__name__}

        def record(r: ChatBotPurgeRequest, guild_id=guild_id, step=step):
            r.steps.setdefault("final_notes", {})[guild_id] = step

        if not await _update_request(session_factory, request_id, run_id, record):
            return False
    return True


async def run_check(
    request_id: UUID,
    *,
    session_factory: SessionFactory,
    redis,
    now: Callable[[], datetime],
    agent=None,
    scan_only: bool = False,
) -> str | None:
    """Final notes pass (once per run), then search every store and settle the request.

    Run by hand mid-run, it only stores a fresh report: it never ends a run
    still waiting for acknowledgements or still in its final notes pass.
    ``scan_only`` (the admin's "Run the check again") never runs the notes
    pass: only the deterministic search, re-settling a finished request.
    """
    async with session_factory() as session:
        await expire_acks(session, request_id, now=now())
    async with session_factory() as session:
        request = await session.get(ChatBotPurgeRequest, request_id)
        if request is None or request.discord_user_id is None or request.run_id is None:
            return None
        run_id = request.run_id
        target = PurgeTarget.build(request.discord_user_id, request.names or [])
        steps = dict(request.steps or {})

    token = str(uuid4())
    finishing = not scan_only and await _update_request(
        session_factory, request_id, run_id, _claim_finishing(token, now())
    )
    if finishing and not await _final_notes_pass(
        request_id, run_id, token, target, steps,
        session_factory=session_factory, now=now, agent=agent,
    ):
        return None

    async with session_factory() as session:
        report = await scan_stores(session, redis, target)
    report["tombstoned"] = await tombstoned_guilds(
        redis, request_id, list(steps.get("guild_ids") or [])
    )
    report["unchecked_names"] = list(target.unchecked_names)
    report["hit_counts"] = hit_counts(report)
    report["checked_at"] = now().isoformat()
    await trim_commands(redis, now=now())

    acknowledged: list[bool] = []

    def settle(request: ChatBotPurgeRequest) -> None:
        request.check_report = report
        if request.status in (STATUS_COMPLETE, STATUS_NEEDS_REVIEW) or (
            finishing and request.status == STATUS_FINISHING and _holds(request.steps, token)
        ):
            request.status = run_outcome(request.steps, report)
            request.completed_at = now()
            request.steps.pop("lease", None)
        # A tombstoned guild keeps its command: the worker retries it.
        if acks_complete(request.steps) and not report["tombstoned"]:
            acknowledged.append(True)
            request.steps["command_entry_id"] = None

    if not await _update_request(session_factory, request_id, run_id, settle):
        return None
    if acknowledged and not await delete_commands_retrying(redis, request_id=request_id):
        # Still on the stream: the next check or close deletes it.
        await _update_request(
            session_factory,
            request_id,
            run_id,
            lambda r: r.steps.__setitem__("command_delete_pending", True),
        )
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
        "information": len(report.get("information") or []),
        "tombstoned": len(report.get("tombstoned") or []),
    }


async def close_request(
    session: AsyncSession, request_id: UUID, *, now: datetime, redis=None
) -> ChatBotPurgeRequest | None:
    """Strip the request to a bare receipt; the caller commits.

    The ID, names, per-guild details and check locations go. What remains is
    when it was asked, when it finished, and counts. The block-list row is
    untouched: it is what keeps the person out of the chat bot from now on.
    Every command this request put on the stream is deleted.
    """
    request = await session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
    if request is None:
        return None
    if redis is not None and request.status != STATUS_CLOSED:
        tombstoned = await tombstoned_guilds(
            redis, request_id, list((request.steps or {}).get("guild_ids") or [])
        )
        if tombstoned:
            raise CloseRefused(tombstoned)
        await delete_commands(redis, request_id=request_id)
    summary = receipt_summary(request)
    request.discord_user_id = None
    request.names = None
    request.steps = {"receipt": summary}
    request.check_report = None
    request.status = STATUS_CLOSED
    request.closed_at = now
    # A run still in flight fails its next check and deletes its own command.
    request.run_id = None
    await session.flush()
    return request
