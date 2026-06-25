"""Shared in-memory state + runner for the Jina Reader cache warm-up.

The admin dashboard at ``/admin/jina-cache`` triggers a precrawl over
``resource_sources`` and watches it in real time via SSE. Both pieces share
the module-level :class:`_RunnerState`: the runner mutates it while a crawl
is in flight; the SSE endpoint reads snapshots and listens on an
``asyncio.Queue`` fanned out per-subscriber.

Only one crawl runs at a time per process. ``start_warm()`` returns False if
a run is already active. Subscribers attach with :meth:`subscribe` and detach
on disconnect; ``broadcast`` is non-blocking and drops events for slow
consumers rather than back-pressuring the runner.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.research_tools import jina_read

logger = logging.getLogger(__name__)

_MAX_CHARS = 10_000
_DEFAULT_TTL_DAYS = 30
_EVENT_LOG_CAP = 200


@dataclass
class _Event:
    """Single point-in-time event emitted by the runner."""

    seq: int
    kind: str  # "started" | "progress" | "finished" | "error" | "snapshot"
    at: datetime
    url: Optional[str] = None
    status: Optional[str] = None  # "ok" | "error" | "skipped" | "empty"
    detail: Optional[str] = None
    duration_ms: Optional[int] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at.isoformat(),
            "url": self.url,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            **self.payload,
        }


@dataclass
class _RunnerState:
    """Process-local crawler state. Single instance lives at module scope."""

    running: bool = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    total: int = 0
    completed: int = 0
    errors: int = 0
    skipped: int = 0
    last_error: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)
    events: deque[_Event] = field(default_factory=lambda: deque(maxlen=_EVENT_LOG_CAP))
    _seq: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _subscribers: set[asyncio.Queue] = field(default_factory=set)

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total": self.total,
            "completed": self.completed,
            "errors": self.errors,
            "skipped": self.skipped,
            "in_flight": sorted(self.in_flight),
            "last_error": self.last_error,
            "options": self.options,
            "last_seq": self._seq,
        }


_state = _RunnerState()


# ---------------------------------------------------------------------------
# Subscriber fan-out
# ---------------------------------------------------------------------------


def subscribe() -> asyncio.Queue:
    """Register a new SSE subscriber queue and return it."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    _state._subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _state._subscribers.discard(queue)


def _broadcast(event: _Event) -> None:
    """Push event into every subscriber queue (non-blocking; drop if full)."""
    _state.events.append(event)
    payload = event.as_dict()
    snapshot = _state.snapshot()
    for q in list(_state._subscribers):
        try:
            q.put_nowait({"event": payload, "state": snapshot})
        except asyncio.QueueFull:
            # Slow consumer — drop rather than block the runner. They'll
            # resync from the snapshot on the next event.
            logger.debug("dropping event for full subscriber queue")


def get_snapshot() -> dict[str, Any]:
    """Return current state + recent events for an initial SSE handshake."""
    return {
        "state": _state.snapshot(),
        "events": [e.as_dict() for e in list(_state.events)[-50:]],
    }


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------


def _is_stale(src: ResourceSource, ttl: timedelta) -> bool:
    if not src.jina_content or not src.jina_fetched_at:
        return True
    return datetime.now(timezone.utc) - src.jina_fetched_at > ttl


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    session_factory,
    src_id,
    url: str,
) -> _Event:
    started = datetime.now(timezone.utc)
    _state.in_flight.add(url)
    try:
        async with sem:
            result = await jina_read(client, url)

        if "error" in result:
            return _Event(
                seq=_state.next_seq(),
                kind="progress",
                at=datetime.now(timezone.utc),
                url=url,
                status="error",
                detail=str(result["error"])[:240],
                duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            )

        title = (result.get("title") or "").strip()
        content = (result.get("content") or "").strip()
        body = f"{title}\n\n{content}".strip()[:_MAX_CHARS]
        if not body:
            return _Event(
                seq=_state.next_seq(),
                kind="progress",
                at=datetime.now(timezone.utc),
                url=url,
                status="empty",
                detail="empty body",
                duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            )

        async with session_factory() as s:
            await s.execute(sql_text("SET search_path TO skrift, public"))
            await s.execute(
                sql_text(
                    "UPDATE resource_sources "
                    "SET jina_content = :body, jina_fetched_at = :now "
                    "WHERE id = :id"
                ),
                {"body": body, "now": datetime.now(timezone.utc), "id": src_id},
            )
            await s.commit()

        return _Event(
            seq=_state.next_seq(),
            kind="progress",
            at=datetime.now(timezone.utc),
            url=url,
            status="ok",
            detail=f"{len(body)} chars",
            duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("warm-up exception for %s", url)
        return _Event(
            seq=_state.next_seq(),
            kind="progress",
            at=datetime.now(timezone.utc),
            url=url,
            status="error",
            detail=f"{type(exc).__name__}: {exc}"[:240],
            duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        )
    finally:
        _state.in_flight.discard(url)


async def _run(
    *,
    force: bool,
    ttl_days: int,
    concurrency: int,
    limit: Optional[int],
) -> None:
    """Walk ``resource_sources`` and warm each row, broadcasting progress."""
    _state.running = True
    _state.started_at = datetime.now(timezone.utc)
    _state.finished_at = None
    _state.total = 0
    _state.completed = 0
    _state.errors = 0
    _state.skipped = 0
    _state.last_error = None
    _state.options = {
        "force": force,
        "ttl_days": ttl_days,
        "concurrency": concurrency,
        "limit": limit,
    }
    _state.in_flight.clear()
    _state.events.clear()
    _state._seq = 0

    _broadcast(
        _Event(
            seq=_state.next_seq(),
            kind="started",
            at=datetime.now(timezone.utc),
            payload={
                "force": force,
                "ttl_days": ttl_days,
                "concurrency": concurrency,
                "limit": limit,
            },
        )
    )

    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    ttl = timedelta(days=ttl_days)
    try:
        async with Session() as s:
            await s.execute(sql_text("SET search_path TO skrift, public"))
            all_sources = (await s.execute(select(ResourceSource))).scalars().all()

        targets = [src for src in all_sources if force or _is_stale(src, ttl)]
        if limit is not None:
            targets = targets[:limit]

        _state.total = len(targets)
        _state.skipped = len(all_sources) - len(targets)
        _broadcast(
            _Event(
                seq=_state.next_seq(),
                kind="snapshot",
                at=datetime.now(timezone.utc),
                payload={
                    "total_sources": len(all_sources),
                    "to_warm": len(targets),
                    "skipped_fresh": _state.skipped,
                },
            )
        )

        if not targets:
            return

        sem = asyncio.Semaphore(max(1, concurrency))
        async with httpx.AsyncClient() as client:
            tasks = [
                asyncio.create_task(_fetch_one(client, sem, Session, src.id, src.url))
                for src in targets
            ]
            for coro in asyncio.as_completed(tasks):
                event = await coro
                _state.completed += 1
                if event.status == "ok":
                    pass
                else:
                    _state.errors += 1
                _broadcast(event)
    except Exception as exc:  # noqa: BLE001
        logger.exception("warm-up crashed")
        _state.last_error = f"{type(exc).__name__}: {exc}"
        _broadcast(
            _Event(
                seq=_state.next_seq(),
                kind="error",
                at=datetime.now(timezone.utc),
                detail=_state.last_error,
            )
        )
    finally:
        await engine.dispose()
        _state.running = False
        _state.finished_at = datetime.now(timezone.utc)
        _broadcast(
            _Event(
                seq=_state.next_seq(),
                kind="finished",
                at=_state.finished_at,
                payload={
                    "completed": _state.completed,
                    "errors": _state.errors,
                    "total": _state.total,
                },
            )
        )


async def start_warm(
    *,
    force: bool = False,
    ttl_days: int = _DEFAULT_TTL_DAYS,
    concurrency: int = 4,
    limit: Optional[int] = None,
) -> bool:
    """Kick off a warm-up if none is in flight. Returns True if started."""
    async with _state._lock:
        if _state.running:
            return False
        _state.running = True  # latched before task starts; _run resets fields
    # Detach the run so the POST returns immediately.
    asyncio.create_task(
        _run(
            force=force,
            ttl_days=ttl_days,
            concurrency=concurrency,
            limit=limit,
        )
    )
    return True


def is_running() -> bool:
    return _state.running
