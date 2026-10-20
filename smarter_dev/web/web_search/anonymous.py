"""Searches from a search link by someone who isn't logged in.

They are never saved. The request and the latest snapshot live in Redis for
``TTL_SECONDS``, long enough for the page to survive a reload, and the worker
runs the usual steps on a ``WebSearchRun`` it never adds to a session
(``UnsavedRun``). Progress reaches only the browser session that started the
search. The usage ledger still records what each search spent, against the
link's owner.

The site Worker runs these searches too, for its own search link page, through
the site search API. It has no browser session here (``nid`` is None): it
reads the search back by id, so the entry also keeps the site's structured
snapshot (``site``), and no notification is sent.

The web pod imports this module, so it must not import pydantic-ai.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID
from uuid import uuid4

from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search.snapshot import EVENT_TYPE
from smarter_dev.web.web_search.snapshot import snapshot
from smarter_dev.web.web_search.snapshot import structured

logger = logging.getLogger(__name__)

JOB_TYPE = "web_search.anonymous"
TTL_SECONDS = 30 * 60
# Per visitor (by IP address), and per link across all its visitors, so a
# link that leaks can't run up its owner's bill.
PER_MINUTE_LIMIT = 2
LINK_PER_DAY_LIMIT = 100
VISITOR_LIMITED = (
    f"Searches without logging in are limited to {PER_MINUTE_LIMIT} a minute."
)
LINK_LIMITED = "This search link has run all its searches without logging in for today."
START_FAILED = "Couldn't start the search. Try again."


# Count one search unless the window is full; 0, or the seconds until the
# window ends. One script, so a refusal has nothing to roll back.
_COUNT = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count < tonumber(ARGV[1]) then
  redis.call('INCR', KEYS[1])
  redis.call('EXPIRE', KEYS[1], ARGV[2], 'NX')
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[2], 'NX')
return math.max(1, redis.call('TTL', KEYS[1]))
"""


class Expired(Exception):
    """The search's 30 minutes ran out while it was still running."""


class Refusal(NamedTuple):
    message: str
    wait: int
    # The link's daily allowance ran out, rather than the visitor's.
    per_link: bool


def _key(search_id: UUID | str) -> str:
    return f"web_search:anonymous:{search_id}"


def unsaved_row(entry: dict) -> WebSearchRun:
    """The search as a row that is never added to a session."""
    state = entry["search"]
    return WebSearchRun(
        id=UUID(state["id"]),
        owner_user_id=UUID(entry["owner_user_id"]),
        submission_key="anonymous",
        request=state["request"],
        status=state["status"],
        queries=[],
        results=[],
        ranked=False,
        needs_answer=False,
        answer=None,
        error=None,
        usage={},
        version=state["version"],
        attempt_count=entry.get("attempts", 0),
        created_at=datetime.fromisoformat(state["created_at"]),
    )


async def start(redis, *, owner_user_id: UUID, nid: str | None, request: str) -> dict:
    """Keep a new anonymous search in Redis; returns its first snapshot."""
    entry = await _store(redis, owner_user_id=owner_user_id, nid=nid, request=request)
    return entry["search"]


async def _store(redis, *, owner_user_id: UUID, nid: str | None, request: str) -> dict:
    row = WebSearchRun(
        id=uuid4(),
        owner_user_id=owner_user_id,
        request=request,
        status="queued",
        queries=[],
        results=[],
        ranked=False,
        needs_answer=False,
        usage={},
        version=0,
        created_at=datetime.now(UTC),
    )
    state = snapshot(row)
    entry = {"owner_user_id": str(owner_user_id), "nid": nid, "search": state}
    if nid is None:
        entry["site"] = structured(row)
    await redis.set(_key(state["id"]), json.dumps(entry), ex=TTL_SECONDS)
    return entry


async def load(redis, search_id: UUID | str) -> dict | None:
    raw = await redis.get(_key(search_id))
    if not raw:
        return None
    entry = json.loads(raw)
    # The key's TTL is the usual clock; this holds even for a key without one.
    # A start time that can't be read can't be trusted: treat it as expired.
    try:
        started = datetime.fromisoformat(entry["search"]["created_at"])
        if datetime.now(UTC) - started < timedelta(seconds=TTL_SECONDS):
            return entry
    except (KeyError, TypeError, ValueError):
        pass
    return None


async def save(redis, entry: dict) -> bool:
    """Write the entry back; False when it has already expired.

    KEEPTTL: the search expires on the clock it started with. XX: a late
    write never brings back an expired search, which would never expire."""
    saved = await redis.set(
        _key(entry["search"]["id"]), json.dumps(entry), xx=True, keepttl=True
    )
    return bool(saved)


async def wait_seconds(redis, key: str, limit: int, window_seconds: int) -> int:
    """Count one search against ``key``; 0 when it is within ``limit``, else
    the seconds until the window ends. A refused search isn't counted."""
    return int(await redis.eval(_COUNT, 1, key, limit, window_seconds))


def link_day_key(owner_user_id: UUID | str) -> str:
    """The daily count of a member's search link. It names the owner, never
    the link: a link made, remade or deleted (here or on next.smarter.dev,
    which keeps links in D1) carries the same day."""
    return f"web_search:anonymous:link:owner:{owner_user_id}"


async def admit(redis, *, visitor: str, owner_user_id: UUID | str) -> Refusal | None:
    """Count one search against its visitor's minute and its link's day.

    None when both allow it. A visitor refused for the minute isn't counted
    against the link."""
    wait = await wait_seconds(
        redis, f"web_search:anonymous:visitor:{visitor}", PER_MINUTE_LIMIT, 60
    )
    if wait:
        return Refusal(VISITOR_LIMITED, wait, per_link=False)
    wait = await wait_seconds(
        redis, link_day_key(owner_user_id), LINK_PER_DAY_LIMIT, 86_400
    )
    if wait:
        return Refusal(LINK_LIMITED, wait, per_link=True)
    return None


async def launch(redis, *, owner_user_id: UUID, nid: str | None, request: str) -> dict:
    """Start an admitted search and queue it on the agent worker; returns its
    entry. A search that couldn't be queued is saved as an error to show."""
    from skrift.workers import submit as worker_submit

    from smarter_dev.web.chat.dispatch import ensure_submission_handler

    entry = await _store(redis, owner_user_id=owner_user_id, nid=nid, request=request)
    state = entry["search"]
    try:
        ensure_submission_handler(JOB_TYPE)
        await worker_submit(
            JOB_TYPE,
            {"search_id": state["id"]},
            queue="agents",
            job_id=f"anonymous:{state['id']}",
        )
    except Exception as error:  # noqa: BLE001 - shown on the search page
        logger.error("Anonymous web search couldn't start (%s)", type(error).__name__)
        saved = await load(redis, state["id"])
        if saved is not None:
            for view in (saved["search"], saved.get("site")):
                if view is not None:
                    view.update(status="error", active=False, error=START_FAILED)
            await save(redis, saved)
            return saved
    return entry


class UnsavedRun:
    """The pipeline's run for an anonymous search: the row lives in memory,
    each step's snapshot goes to Redis and to the session that started it."""

    def __init__(self, redis, entry: dict) -> None:
        self.redis = redis
        self.entry = entry
        self.nid = entry["nid"]
        self.row = unsaved_row(entry)
        self.run_id = self.row.id

    async def read(self) -> WebSearchRun:
        return self.row

    async def update(self, change) -> dict:
        from skrift.notifications import NotificationMode
        from skrift.notifications import notify_session

        change(self.row)
        self.row.version += 1
        state = snapshot(self.row)
        self.entry["search"] = state
        self.entry["attempts"] = self.row.attempt_count
        if self.nid is None:
            self.entry["site"] = structured(self.row)
        if not await save(self.redis, self.entry):
            raise Expired(str(self.run_id))
        if self.nid is None:
            # The site reads the search back by id; there is no session to tell.
            return state
        try:
            await notify_session(
                self.nid, EVENT_TYPE, mode=NotificationMode.EPHEMERAL, search=state
            )
        except Exception as error:  # noqa: BLE001 - the page re-reads Redis on reconnect
            # Type only: the payload holds the request and results.
            logger.error(
                "Anonymous web search notification failed (%s)", type(error).__name__
            )
        return state

    async def record(self, rows) -> None:
        from smarter_dev.web.web_search import metering

        await metering.record_unsaved(self.row, rows)
