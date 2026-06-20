"""Skrift admin: blogging-agent dashboard.

Stage 1 surfaces candidate blog topics filed by the Discord chat agent
(``/admin/blogging-agent/topics``). Stage 2 adds the authoring pipeline:
admin triggers a multi-stage run, watches its audit log in real time, and
toggles the resulting post out of `noindex` once it's ready for the
public web (``/admin/blogging-agent/runs``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException
from litestar.params import Body, Parameter
from litestar.response import Redirect, ServerSentEvent
from litestar.response import Template as TemplateResponse
from sqlalchemy import desc, func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.db.models.user import User
from skrift.db.models.page import Page
from skrift.flash import flash_error, flash_success, get_flash_messages
from skrift.workers import submit as worker_submit
from skrift.workers.runtime import get_runtime

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
from smarter_dev.web.blogging_agent.pipeline import PipelineRunPayload
from smarter_dev.web.models import (
    AuthoringPipelineRun,
    CandidateBlogTopic,
    ChatAgentEngagement,
)


def _build_stream_engine():
    """A private async engine for the SSE poller. NullPool so we don't keep
    pooled connections busy while the stream sits idle between events."""
    settings = get_settings()
    return create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )

logger = logging.getLogger(__name__)

VALID_STATUSES: frozenset[str] = frozenset(
    {"new", "kept", "drafted", "discarded"}
)
VALID_CATEGORIES: frozenset[str] = frozenset(
    {"concept", "misconception", "news"}
)


class BloggingAgentAdminController(Controller):
    """Operator dashboard for blog topic candidates surfaced by agents."""

    path = "/admin/blogging-agent"
    guards = [auth_guard]

    @get(
        "/topics",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-bot")],
        opt={
            "label": "Blogging Agent",
            "icon": "edit-circle",
            "order": 66,
        },
    )
    async def list_topics(
        self,
        request: Request,
        db_session: AsyncSession,
        status: Annotated[str | None, Parameter(query="status")] = None,
        category: Annotated[str | None, Parameter(query="category")] = None,
        page: Annotated[int, Parameter(query="page")] = 1,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        page_size = 25
        page = max(1, page)

        summary_stmt = (
            select(CandidateBlogTopic.status, func.count(CandidateBlogTopic.id))
            .group_by(CandidateBlogTopic.status)
        )
        status_counts_rows = (await db_session.execute(summary_stmt)).all()
        status_counts: dict[str, int] = {
            s: 0 for s in ("new", "kept", "drafted", "discarded")
        }
        for s, c in status_counts_rows:
            status_counts[s] = int(c)

        stmt = (
            select(CandidateBlogTopic, ChatAgentEngagement)
            .outerjoin(
                ChatAgentEngagement,
                ChatAgentEngagement.id == CandidateBlogTopic.engagement_id,
            )
            .order_by(desc(CandidateBlogTopic.surfaced_at))
        )
        if status and status in VALID_STATUSES:
            stmt = stmt.where(CandidateBlogTopic.status == status)
        if category and category in VALID_CATEGORIES:
            stmt = stmt.where(CandidateBlogTopic.category == category)

        offset = (page - 1) * page_size
        stmt = stmt.limit(page_size).offset(offset)
        rows = (await db_session.execute(stmt)).all()
        topics = [
            {"topic": topic, "engagement": engagement}
            for topic, engagement in rows
        ]

        count_stmt = select(func.count(CandidateBlogTopic.id))
        if status and status in VALID_STATUSES:
            count_stmt = count_stmt.where(CandidateBlogTopic.status == status)
        if category and category in VALID_CATEGORIES:
            count_stmt = count_stmt.where(CandidateBlogTopic.category == category)
        total = (await db_session.execute(count_stmt)).scalar() or 0
        total_pages = max(1, (total + page_size - 1) // page_size)

        return TemplateResponse(
            "admin/blogging-agent/list.html",
            context={
                "topics": topics,
                "selected_status": status or "",
                "selected_category": category or "",
                "status_counts": status_counts,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "valid_statuses": sorted(VALID_STATUSES),
                "valid_categories": sorted(VALID_CATEGORIES),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/topics/{topic_id:uuid}",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def topic_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        topic_id: UUID,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        stmt = (
            select(CandidateBlogTopic, ChatAgentEngagement, User)
            .outerjoin(
                ChatAgentEngagement,
                ChatAgentEngagement.id == CandidateBlogTopic.engagement_id,
            )
            .outerjoin(
                User,
                User.id == CandidateBlogTopic.reviewed_by_user_id,
            )
            .where(CandidateBlogTopic.id == topic_id)
        )
        row = (await db_session.execute(stmt)).first()
        if row is None:
            raise NotFoundException(detail="Topic not found")
        topic, engagement, reviewer = row

        return TemplateResponse(
            "admin/blogging-agent/detail.html",
            context={
                "topic": topic,
                "engagement": engagement,
                "reviewer": reviewer,
                "valid_statuses": sorted(VALID_STATUSES),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/topics/{topic_id:uuid}/status",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def topic_set_status(
        self,
        request: Request,
        db_session: AsyncSession,
        topic_id: UUID,
        data: Annotated[
            dict, Body(media_type=RequestEncodingType.URL_ENCODED)
        ],
    ) -> Redirect:
        new_status = (data.get("status") or "").strip()
        if new_status not in VALID_STATUSES:
            flash_error(request, f"Unknown status: {new_status!r}.")
            return Redirect(path=f"/admin/blogging-agent/topics/{topic_id}")

        topic = await db_session.get(CandidateBlogTopic, topic_id)
        if topic is None:
            raise NotFoundException(detail="Topic not found")

        ctx = await get_admin_context(request, db_session)
        reviewer = ctx["user"]
        topic.status = new_status
        # Going BACK to 'new' clears the review stamp; any other status records
        # who acted and when.
        if new_status == "new":
            topic.reviewed_at = None
            topic.reviewed_by_user_id = None
        else:
            topic.reviewed_at = datetime.now(timezone.utc)
            topic.reviewed_by_user_id = reviewer.id

        await db_session.commit()
        flash_success(request, f"Topic marked {new_status}.")
        return Redirect(path=f"/admin/blogging-agent/topics/{topic_id}")

    # ── Authoring pipeline runs ─────────────────────────────────────────

    @get(
        "/runs",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-bot")],
        opt={
            "label": "Pipeline runs",
            "icon": "rocket",
            "order": 67,
        },
    )
    async def list_runs(
        self,
        request: Request,
        db_session: AsyncSession,
        page: Annotated[int, Parameter(query="page")] = 1,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        page_size = 25
        page = max(1, page)

        stmt = (
            select(AuthoringPipelineRun, Page, User)
            .outerjoin(Page, Page.id == AuthoringPipelineRun.result_page_id)
            .outerjoin(
                User, User.id == AuthoringPipelineRun.kicked_off_by_user_id
            )
            .order_by(desc(AuthoringPipelineRun.created_at))
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        rows = (await db_session.execute(stmt)).all()
        runs = [
            {"run": run, "page": page_row, "user": user}
            for run, page_row, user in rows
        ]

        total = (
            await db_session.execute(
                select(func.count(AuthoringPipelineRun.id))
            )
        ).scalar() or 0

        return TemplateResponse(
            "admin/blogging-agent/runs_list.html",
            context={
                "runs": runs,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/runs",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def create_run(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Redirect:
        """Kick off a new authoring pipeline run."""
        ctx = await get_admin_context(request, db_session)
        run = AuthoringPipelineRun(
            status="queued",
            kicked_off_by_user_id=ctx["user"].id,
        )
        db_session.add(run)
        await db_session.commit()
        await db_session.refresh(run)

        await worker_submit(PipelineRunPayload(run_id=run.id))
        flash_success(
            request,
            "Pipeline run queued. The detail page updates live as stages finish.",
        )
        return Redirect(path=f"/admin/blogging-agent/runs/{run.id}")

    @get(
        "/runs/{run_id:uuid}",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def run_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        run_id: UUID,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        stmt = (
            select(AuthoringPipelineRun, Page, User)
            .outerjoin(Page, Page.id == AuthoringPipelineRun.result_page_id)
            .outerjoin(
                User, User.id == AuthoringPipelineRun.kicked_off_by_user_id
            )
            .where(AuthoringPipelineRun.id == run_id)
        )
        row = (await db_session.execute(stmt)).first()
        if row is None:
            raise NotFoundException(detail="Pipeline run not found")
        run, page_row, kicked_off_by = row

        # Backfill the audit timeline from Skrift's event log. For each
        # stage stream, walk forward looking for ``SubAgentDispatched``
        # events; when we see one, recursively load that child's stream
        # too so the UI can show its events as their own card. Without
        # this, the researcher sub-agent's tool calls would be invisible
        # to the admin since only stage session ids land on the run row.
        stages = _stage_order(run.stage_session_ids or {})
        stage_events: list[dict] = []
        runtime = get_runtime()
        await _collect_audit(
            runtime=runtime,
            initial_streams=[(stage_name, sid) for stage_name, sid in stages],
            sink=stage_events,
        )

        return TemplateResponse(
            "admin/blogging-agent/runs_detail.html",
            context={
                "run": run,
                "page": page_row,
                "kicked_off_by": kicked_off_by,
                "stages": stages,
                "stage_events": stage_events,
                "is_running": run.status in {"queued", "running"},
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/runs/{run_id:uuid}/stream",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def run_stream(
        self,
        request: Request,
        db_session: AsyncSession,
        run_id: UUID,
    ) -> ServerSentEvent:
        """Server-sent events: tail the run's audit log live.

        Subscribes to every stage's Skrift event stream and yields events as
        they arrive. Closes when the run hits a terminal status.

        We deliberately open our **own** SQLAlchemy engine inside the
        generator. The request-scoped ``db_session`` Litestar injects gets
        torn down before the streaming response finishes, which detaches
        the ``run`` ORM instance and breaks ``refresh()``.
        """
        # Pre-flight: make sure the run actually exists. Use the injected
        # session for the cheap one-shot check; everything inside `gen()`
        # uses a private engine.
        run = await db_session.get(AuthoringPipelineRun, run_id)
        if run is None:
            raise NotFoundException(detail="Pipeline run not found")

        async def gen():
            runtime = get_runtime()
            # Private engine — kept alive for the duration of the stream
            # so we can poll the run row without leaning on the request scope.
            engine = _build_stream_engine()
            Session = async_sessionmaker(engine, expire_on_commit=False)

            seen_streams: set[str] = set()
            tasks: list[asyncio.Task] = []
            queue: asyncio.Queue = asyncio.Queue()

            def subscribe_known(stage_session_ids: dict) -> None:
                """Subscribe to any new stage session ids that have appeared."""
                for stage, sid in stage_session_ids.items():
                    if sid:
                        _maybe_tail(stage, str(sid))

            def _maybe_tail(stage: str, sid: str) -> None:
                if not sid or sid in seen_streams:
                    return
                seen_streams.add(sid)
                tasks.append(asyncio.create_task(tail(stage, sid)))

            async def tail(stage: str, sid: str) -> None:
                stream = f"agents:run:{sid}"
                async for position, event in runtime.event_log.subscribe(
                    stream, from_position=0
                ):
                    await queue.put(
                        {
                            "stage": stage,
                            "session_id": sid,
                            "position": position,
                            "ts": event.get("ts"),
                            "seq": event.get("seq"),
                            "type": event.get("type"),
                            "payload": _json_safe(event.get("payload") or {}),
                        }
                    )
                    # Follow sub-agent dispatches dynamically — researcher
                    # sub-agent (and any future nested agents) emit their
                    # own audit streams that aren't on the run row.
                    child = _child_session_from_event(event)
                    if child is not None:
                        child_sid, child_label = child
                        _maybe_tail(child_label or stage, child_sid)

            async def fetch_run_state() -> dict | None:
                async with Session() as session:
                    fresh = await session.get(AuthoringPipelineRun, run_id)
                    if fresh is None:
                        return None
                    return {
                        "status": fresh.status,
                        "stage_session_ids": dict(fresh.stage_session_ids or {}),
                        "result_page_id": str(fresh.result_page_id)
                        if fresh.result_page_id
                        else None,
                        "error": fresh.error,
                    }

            try:
                # Seed subscriptions from the initial run state.
                state = await fetch_run_state()
                if state is None:
                    return
                subscribe_known(state["stage_session_ids"])

                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                        yield {"event": "audit", "data": json.dumps(event)}
                    except asyncio.TimeoutError:
                        pass

                    state = await fetch_run_state()
                    if state is None:
                        break
                    subscribe_known(state["stage_session_ids"])
                    yield {"event": "status", "data": json.dumps(state)}
                    if state["status"] in {"completed", "failed"}:
                        # Drain whatever's still on the queue before closing.
                        for _ in range(20):
                            try:
                                event = await asyncio.wait_for(
                                    queue.get(), timeout=0.2
                                )
                                yield {
                                    "event": "audit",
                                    "data": json.dumps(event),
                                }
                            except asyncio.TimeoutError:
                                break
                        break
            finally:
                for t in tasks:
                    t.cancel()
                await engine.dispose()

        return ServerSentEvent(gen())

    @post(
        "/runs/{run_id:uuid}/publish",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def run_publish(
        self,
        request: Request,
        db_session: AsyncSession,
        run_id: UUID,
    ) -> Redirect:
        """Clear meta_robots noindex on the run's resulting page."""
        run = await db_session.get(AuthoringPipelineRun, run_id)
        if run is None or run.result_page_id is None:
            flash_error(request, "Run has no associated post to publish.")
            return Redirect(path=f"/admin/blogging-agent/runs/{run_id}")

        await db_session.execute(
            text("UPDATE pages SET meta_robots = NULL WHERE id = :pid").bindparams(
                pid=run.result_page_id
            )
        )
        await db_session.commit()
        flash_success(request, "Post is now indexable.")
        return Redirect(path=f"/admin/blogging-agent/runs/{run_id}")


_STAGE_ORDER = ("review", "scout", "brainstorm", "research", "synthesis")


def _stage_order(stage_session_ids: dict) -> list[tuple[str, str]]:
    """Return (stage_name, session_id) pairs in canonical pipeline order."""
    return [
        (stage, str(stage_session_ids[stage]))
        for stage in _STAGE_ORDER
        if stage_session_ids.get(stage)
    ]


async def _collect_audit(
    *,
    runtime,
    initial_streams: list[tuple[str, str]],
    sink: list[dict],
) -> None:
    """Read every stage stream + recursively follow sub-agent dispatches.

    Mutates ``sink`` in place. Order isn't guaranteed across streams; the
    timeline UI groups by stage/session id and orders within each stream
    by ``seq``.
    """
    seen: set[str] = set()
    work: list[tuple[str, str]] = list(initial_streams)
    while work:
        stage_name, session_id = work.pop(0)
        if session_id in seen:
            continue
        seen.add(session_id)
        stream = f"agents:run:{session_id}"
        try:
            events = await runtime.event_log.read(stream, from_position=0)
        except Exception:  # noqa: BLE001
            logger.exception(
                "event_log.read failed for stage=%s session_id=%s",
                stage_name,
                session_id,
            )
            continue
        for position, event in events:
            payload = event.get("payload") or {}
            sink.append(
                {
                    "position": position,
                    "stage": stage_name,
                    "session_id": session_id,
                    "type": event.get("type"),
                    "payload": _json_safe(payload),
                    "ts": event.get("ts"),
                    "seq": event.get("seq"),
                }
            )
            child = _child_session_from_event(event)
            if child is not None:
                child_sid, child_label = child
                if child_sid not in seen:
                    work.append((child_label or stage_name, child_sid))


def _child_session_from_event(event: dict) -> tuple[str, str | None] | None:
    """Return (child_session_id, derived_stage_label) when an event spawns
    a sub-agent, else None."""
    if "SubAgent" not in (event.get("type") or ""):
        return None
    payload = event.get("payload") or {}
    child_sid = payload.get("child_session_id")
    if not child_sid:
        return None
    name = payload.get("child_agent_name") or ""
    # `blogging.researcher_subagent` → `researcher_subagent` for nicer display.
    label = name.rsplit(".", 1)[-1] if name else None
    return str(child_sid), label


def _json_safe(value):
    """Recursively convert a value into JSON-serialisable primitives.

    Skrift's event log stores rich payloads — Pydantic models, UUIDs,
    datetimes — that don't survive the stdlib json encoder. We dump
    everything to plain dicts/strings before templating or yielding over
    SSE so neither side trips.
    """
    from datetime import date, datetime  # local import; tight hot path
    from decimal import Decimal
    from uuid import UUID

    from pydantic import BaseModel

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    # Last resort — coerce unknown shapes to repr so the page renders
    # something legible instead of 500-ing.
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return f"<unrenderable {type(value).__name__}>"
