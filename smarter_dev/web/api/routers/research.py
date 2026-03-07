"""FastAPI router for the Scan research API.

Endpoints:
    POST   /research              — Start a new research session
    GET    /research/{id}/stream  — SSE event stream (bridges Skrift SourceRegistry)
    GET    /research/{id}         — Get full result JSON
    POST   /research/{id}/followup — Start a follow-up research session
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from skrift.lib.notifications import notifications as notification_service
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import get_database_session, verify_api_key
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.runner import start_research_task
from smarter_dev.web.scan.schemas import (
    FollowupRequest,
    ResearchRequest,
    ResearchResponse,
    ResearchSessionSchema,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research", tags=["Research"])
ops = ResearchSessionOperations()


@router.post("", response_model=ResearchResponse)
async def create_research(
    body: ResearchRequest,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_database_session),
):
    """Start a new research session.

    Returns immediately with a session_id. The agent runs in the background.
    Use the stream endpoint to follow progress in real time.
    """
    session = await ops.create_session(
        db,
        query=body.query,
        user_id=body.user_id,
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        context=body.context,
    )
    await db.commit()

    session_id = session.id
    source_key = f"research:{session_id}"

    start_research_task(
        session_id=session_id,
        query=body.query,
        user_id=body.user_id,
        context=body.context,
    )

    return ResearchResponse(
        session_id=session_id,
        source_key=source_key,
        stream_url=f"/api/research/{session_id}/stream",
    )


@router.get("/{session_id}/stream")
async def stream_research(
    session_id: UUID,
    request: Request,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_database_session),
):
    """SSE stream of research events for the given session.

    Bridges Skrift's SourceRegistry — adds a listener queue on the
    ``research:{session_id}`` source and forwards notifications as SSE.

    Events: status, tool_use, tool_result, complete, error.
    The stream closes after a complete or error event.
    """
    session = await ops.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Research session not found")

    # If already finished, return final state as a single SSE event
    if session.status in ("complete", "error"):
        event_type = session.status
        if session.status == "complete":
            data = {
                "result_id": str(session.id),
                "result_url": f"https://scan.smarter.dev/r/{session.id}",
                "summary": session.summary or "",
            }
        else:
            data = {"error": session.error_message or "Unknown error", "recoverable": False}

        async def _finished():
            yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

        return StreamingResponse(_finished(), media_type="text/event-stream")

    # Subscribe a queue to the research source via Skrift's SourceRegistry
    source_key = f"research:{session_id}"
    registry = notification_service._registry
    queue: asyncio.Queue = asyncio.Queue()
    registry.add_listener(source_key, queue)

    async def _stream():
        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    notification = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                # Notification is a skrift Notification dataclass
                event_type = notification.type
                payload = notification.payload or {}
                yield f"event: {event_type}\ndata: {json.dumps(payload, default=str)}\n\n"

                if event_type in ("complete", "error"):
                    break
        finally:
            registry.remove_listener(source_key, queue)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/{session_id}", response_model=ResearchSessionSchema)
async def get_research(
    session_id: UUID,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_database_session),
):
    """Get the full result of a research session."""
    session = await ops.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Research session not found")
    return session


@router.post("/{session_id}/followup", response_model=ResearchResponse)
async def create_followup(
    session_id: UUID,
    body: FollowupRequest,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_database_session),
):
    """Start a follow-up research session linked to a parent session."""
    parent = await ops.get_session(db, session_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent research session not found")

    context = {
        "parent_id": str(session_id),
        "parent_query": parent.query,
        "parent_summary": parent.summary or "",
    }

    followup = await ops.create_session(
        db,
        query=body.query,
        user_id=parent.user_id,
        guild_id=parent.guild_id,
        channel_id=parent.channel_id,
        context=context,
    )
    await db.commit()

    await ops.add_followup(db, session_id, followup.id, body.query)

    followup_id = followup.id
    source_key = f"research:{followup_id}"

    start_research_task(
        session_id=followup_id,
        query=body.query,
        user_id=parent.user_id,
        context=context,
    )

    return ResearchResponse(
        session_id=followup_id,
        source_key=source_key,
        stream_url=f"/api/research/{followup_id}/stream",
    )
