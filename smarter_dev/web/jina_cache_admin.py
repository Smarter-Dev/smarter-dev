"""Admin dashboard for the Jina Reader cache warm-up.

* ``GET /admin/jina-cache`` — dashboard with current totals, last-run
  status, and live progress (hydrated via SSE).
* ``POST /admin/jina-cache/start`` — kick off a warm-up. Accepts ``force``,
  ``ttl_days``, ``concurrency``, and ``limit`` form fields. Returns a
  redirect with a flash; the dashboard auto-streams from there.
* ``GET /admin/jina-cache/stream`` — SSE stream of events from
  :mod:`smarter_dev.web.jina_cache_runner`.

Only one warm-up runs per process. The SSE endpoint sends an initial
``snapshot`` event with the cached recent events so a refresh in the
middle of a run picks up where it left off.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Optional

from litestar import Controller, Request, get, post
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission
from skrift.lib.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.jina_cache_runner import (
    get_snapshot,
    is_running,
    start_warm,
    subscribe,
    unsubscribe,
)
from smarter_dev.web.models import ResourceSource

logger = logging.getLogger(__name__)

_STALE_AFTER_DAYS = 30


class JinaCacheAdminController(Controller):
    """Live admin dashboard for the Jina cache warm-up runner."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/jina-cache",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Jina Cache", "icon": "database", "order": 65},
    )
    async def jina_cache_dashboard(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        threshold = datetime.now(timezone.utc) - timedelta(days=_STALE_AFTER_DAYS)

        totals_q = await db_session.execute(
            select(
                func.count(ResourceSource.id),
                func.count(ResourceSource.jina_content),
                func.count()
                .filter(ResourceSource.jina_fetched_at >= threshold)
                .label("fresh"),
            )
        )
        total_sources, cached_count, fresh_count = totals_q.one()

        stale_count = (cached_count or 0) - (fresh_count or 0)
        missing_count = (total_sources or 0) - (cached_count or 0)

        recent_q = await db_session.execute(
            select(
                ResourceSource.url,
                ResourceSource.title,
                ResourceSource.jina_fetched_at,
                func.length(ResourceSource.jina_content).label("body_len"),
            )
            .where(ResourceSource.jina_fetched_at.is_not(None))
            .order_by(ResourceSource.jina_fetched_at.desc())
            .limit(20)
        )
        recent_rows = [
            {
                "url": r.url,
                "title": r.title,
                "fetched_at": r.jina_fetched_at,
                "body_len": r.body_len or 0,
            }
            for r in recent_q.all()
        ]

        snapshot = get_snapshot()

        return TemplateResponse(
            "admin/jina_cache.html",
            context={
                "flash_messages": get_flash_messages(request),
                "totals": {
                    "total": total_sources or 0,
                    "cached": cached_count or 0,
                    "fresh": fresh_count or 0,
                    "stale": stale_count if stale_count >= 0 else 0,
                    "missing": missing_count if missing_count >= 0 else 0,
                },
                "stale_after_days": _STALE_AFTER_DAYS,
                "recent": recent_rows,
                "runner": snapshot,
                "running": is_running(),
                **ctx,
            },
        )

    @post(
        "/jina-cache/start",
        guards=[auth_guard, Permission("administrator")],
    )
    async def jina_cache_start(self, request: Request) -> Redirect:
        form = await request.form()

        def _int(name: str, default: Optional[int]) -> Optional[int]:
            raw = (form.get(name) or "").strip()
            if not raw:
                return default
            try:
                v = int(raw)
                return v if v > 0 else default
            except ValueError:
                return default

        force = (form.get("force") or "").lower() in ("1", "true", "on", "yes")
        ttl_days = _int("ttl_days", 30) or 30
        concurrency = _int("concurrency", 4) or 4
        limit = _int("limit", None)

        started = await start_warm(
            force=force,
            ttl_days=ttl_days,
            concurrency=concurrency,
            limit=limit,
        )
        if started:
            flash_success(
                request,
                f"Warm-up started (force={force}, concurrency={concurrency}"
                + (f", limit={limit}" if limit else "")
                + ").",
            )
        else:
            flash_error(request, "A warm-up is already running.")
        return Redirect(path="/admin/jina-cache")

    @get(
        "/jina-cache/stream",
        guards=[auth_guard, Permission("administrator")],
    )
    async def jina_cache_stream(self, request: Request) -> ServerSentEvent:
        queue = subscribe()

        async def generate() -> AsyncGenerator[ServerSentEventMessage, None]:
            try:
                yield ServerSentEventMessage(
                    data=json.dumps(get_snapshot(), default=str),
                    event="snapshot",
                )

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ServerSentEventMessage(comment="keepalive")
                        continue

                    yield ServerSentEventMessage(
                        data=json.dumps(msg, default=str),
                        event="update",
                    )
            finally:
                unsubscribe(queue)

        return ServerSentEvent(generate())
