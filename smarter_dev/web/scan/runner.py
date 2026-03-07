"""Background task orchestrator for research sessions.

Runs the Pydantic AI agent, emits timeseries notifications via Skrift's
SourceRegistry, and persists results to the database.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

import httpx
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    TextPartDelta,
)
from skrift.lib.notifications import NotificationMode, notify_source

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.scan.agent import MODEL, ResearchDeps, ResearchResult, research_agent
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.tools import URLRateLimiter

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()


def _source_key(session_id: str) -> str:
    return f"research:{session_id}"


async def _emit(session_id: str, event_type: str, **payload: object) -> None:
    """Emit a timeseries notification on the research source."""
    await notify_source(
        _source_key(session_id),
        event_type,
        mode=NotificationMode.TIMESERIES,
        **payload,
    )


async def run_research(
    session_id: UUID,
    query: str,
    user_id: str,
    context: dict | None = None,
) -> None:
    """Run the research agent as a background task.

    All progress is emitted as timeseries notifications on the
    ``research:{session_id}`` source key. Clients (API SSE, web UI)
    subscribe to that source to receive live updates.
    """
    sid = str(session_id)
    start_time = time.monotonic()

    await _emit(sid, "status", stage="planning", message="Analyzing query...")

    tool_log: list[dict] = []

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Smarter Dev Scan Agent - admin@smarter.dev"},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                url_rate_limiter=URLRateLimiter(),
            )

            result_data: ResearchResult | None = None

            async for event in research_agent.run_stream_events(
                query, deps=deps, model=MODEL
            ):
                if isinstance(event, FunctionToolCallEvent):
                    tool_name = event.part.tool_name
                    tool_args = event.part.args
                    await _emit(
                        sid, "tool_use",
                        tool=tool_name,
                        input=tool_args if isinstance(tool_args, dict) else {},
                        status="running",
                    )
                    tool_log.append({
                        "tool": tool_name,
                        "input": tool_args if isinstance(tool_args, dict) else {},
                        "status": "running",
                    })

                elif isinstance(event, FunctionToolResultEvent):
                    tool_name = event.result.tool_name
                    content = str(event.result.content)[:5120]
                    await _emit(
                        sid, "tool_result",
                        tool=tool_name,
                        status="complete",
                        content=content,
                    )
                    for entry in reversed(tool_log):
                        if entry["tool"] == tool_name and entry["status"] == "running":
                            entry["status"] = "complete"
                            entry["content"] = content
                            break

                elif isinstance(event, PartDeltaEvent):
                    if isinstance(event.delta, TextPartDelta):
                        await _emit(
                            sid, "response_chunk",
                            delta=event.delta.content_delta,
                        )

                elif isinstance(event, FinalResultEvent):
                    await _emit(
                        sid, "status",
                        stage="synthesizing",
                        message="Composing response...",
                    )

                elif isinstance(event, AgentRunResultEvent):
                    result_data = event.result.output

            if result_data is None:
                raise RuntimeError("Agent completed without producing a result")

            duration = time.monotonic() - start_time

            # Persist to DB
            async with get_db_session_context() as db_session:
                await ops.update_session_result(
                    db_session,
                    session_id,
                    response=result_data.response,
                    summary=result_data.summary,
                    sources=[s.model_dump() for s in result_data.sources],
                    tool_log=tool_log,
                )

            # Emit completion
            await _emit(
                sid, "complete",
                result_id=sid,
                result_url=f"https://scan.smarter.dev/r/{sid}",
                summary=result_data.summary,
                response=result_data.response,
                duration=round(duration, 2),
            )

    except Exception as e:
        logger.exception("Research session %s failed", sid)
        error_msg = f"{type(e).__name__}: {e}"

        try:
            async with get_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, error_msg)
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)

        await _emit(sid, "error", error=error_msg, recoverable=False)


def start_research_task(
    session_id: UUID,
    query: str,
    user_id: str,
    context: dict | None = None,
) -> asyncio.Task:
    """Create and return an asyncio.Task for the research agent."""
    return asyncio.create_task(
        run_research(session_id, query, user_id, context),
        name=f"research:{session_id}",
    )
