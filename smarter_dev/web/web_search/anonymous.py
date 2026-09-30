"""Searches from a search link by someone who isn't logged in.

They are never saved. The request and the latest snapshot live in Redis for
``TTL_SECONDS``, long enough for the page to survive a reload, and the worker
runs the usual steps on a ``WebSearchRun`` it never adds to a session
(``UnsavedRun``). Progress reaches only the browser session that started the
search. The usage ledger still records what each search spent, against the
link's owner.

The web pod imports this module, so it must not import pydantic-ai.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search.snapshot import EVENT_TYPE
from smarter_dev.web.web_search.snapshot import snapshot

logger = logging.getLogger(__name__)

JOB_TYPE = "web_search.anonymous"
TTL_SECONDS = 30 * 60
# Per visitor (by IP address), and per link across all its visitors, so a
# link that leaks can't run up its owner's bill.
PER_MINUTE_LIMIT = 2
LINK_PER_DAY_LIMIT = 100


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


async def start(redis, *, owner_user_id: UUID, nid: str, request: str) -> dict:
    """Keep a new anonymous search in Redis; returns its first snapshot."""
    state = snapshot(
        WebSearchRun(
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
    )
    entry = {"owner_user_id": str(owner_user_id), "nid": nid, "search": state}
    await redis.set(_key(state["id"]), json.dumps(entry), ex=TTL_SECONDS)
    return state


async def load(redis, search_id: UUID | str) -> dict | None:
    raw = await redis.get(_key(search_id))
    return json.loads(raw) if raw else None


async def save(redis, entry: dict) -> None:
    # KEEPTTL: the search expires on the clock it started with.
    await redis.set(_key(entry["search"]["id"]), json.dumps(entry), keepttl=True)


async def wait_seconds(redis, key: str, limit: int, window_seconds: int) -> int:
    """Count one search against ``key``; 0 when it is within ``limit``, else
    the seconds until the window ends. A refused search isn't counted."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        pipe.ttl(key)
        count, _, ttl = await pipe.execute()
    if int(count) <= limit:
        return 0
    await redis.decr(key)
    return max(1, int(ttl))


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
        await save(self.redis, self.entry)
        try:
            await notify_session(
                self.nid, EVENT_TYPE, mode=NotificationMode.EPHEMERAL, search=state
            )
        except Exception as error:  # noqa: BLE001 - the page re-reads Redis on reconnect
            # Type only: the payload holds the request and results.
            logger.error("Anonymous web search notification failed (%s)", type(error).__name__)
        return state

    async def record(self, rows) -> None:
        from smarter_dev.web.web_search import metering

        await metering.record_unsaved(self.row, rows)
