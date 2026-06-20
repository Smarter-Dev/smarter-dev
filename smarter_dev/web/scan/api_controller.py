"""Litestar controller for the Scan research API.

Single POST endpoint that accepts a research query and returns an SSE stream
of tool uses and the final result. Uses Skrift API key authentication.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from litestar import Controller, Request, post
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage
from pydantic import BaseModel, Field
from skrift.auth.guards import APIKeyOnly, auth_guard
from skrift.notifications import notifications as notification_service
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.runner import start_research_task

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()


class ResearchAPIRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    user_id: str | None = None
    context: dict | None = None
    mode: str = "auto"


class ScanAPIController(Controller):
    path = "/api/research"

    @post(
        "/",
        guards=[auth_guard, APIKeyOnly()],
    )
    async def research(
        self,
        request: Request,
        db_session: AsyncSession,
        data: ResearchAPIRequest,
    ) -> ServerSentEvent:
        """Start a research session and stream events as SSE.

        Accepts a query and optional Discord user_id. Returns an SSE stream
        with events: status, tool_use, tool_result, complete, error.
        """
        session = await ops.create_session(
            db_session,
            query=data.query,
            user_id=data.user_id,
            context=data.context,
            pipeline_mode=data.mode,
        )
        await db_session.commit()

        session_id = session.id
        sid = str(session_id)

        # For notification routing: use the real user_id if provided,
        # otherwise use a session-scoped sentinel so the runner's _emit
        # helper publishes to a unique channel we can listen on.
        effective_user_id = data.user_id or f"api:{sid}"

        start_research_task(
            session_id=session_id,
            query=data.query,
            user_id=effective_user_id,
            mode=data.mode,
        )

        registry = notification_service._registry
        listener_key = f"user:{effective_user_id}"
        queue: asyncio.Queue = asyncio.Queue()
        registry.add_listener(listener_key, queue)

        async def generate() -> AsyncGenerator[ServerSentEventMessage, None]:
            try:
                # Emit the session_id so the client knows what was created
                yield ServerSentEventMessage(
                    data=json.dumps({"session_id": sid}),
                    event="session_created",
                )

                while True:
                    try:
                        notification = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield ServerSentEventMessage(comment="keepalive")
                        continue

                    # Only forward research notifications for this session
                    if not notification.type.startswith("research:"):
                        continue
                    payload = notification.payload or {}
                    if payload.get("session_id") != sid:
                        continue

                    event_type = notification.type.removeprefix("research:")
                    yield ServerSentEventMessage(
                        data=json.dumps(payload, default=str),
                        event=event_type,
                    )

                    if event_type in ("complete", "error"):
                        break
            finally:
                registry.remove_listener(listener_key, queue)

        return ServerSentEvent(generate())
