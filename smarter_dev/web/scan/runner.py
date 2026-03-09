"""Background task orchestrator for research sessions.

Runs the Pydantic AI agent, emits user-scoped notifications via Skrift's
notification system, and persists results to the database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import zoneinfo
from datetime import datetime
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
from pydantic_ai.usage import RunUsage
from skrift.lib.notifications import NotificationMode, notify_user

from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.scan.agent import (
    CODE_EXAMPLES_MODEL,
    MODEL,
    SYSTEM_PROMPT,
    ResearchDeps,
    ResearchResult,
    generate_code_examples,
    generate_session_meta,
    generate_youtube_query,
    rank_youtube_results,
    research_agent,
    run_lite_pipeline,
)
from smarter_dev.web.scan.tools import youtube_search
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.pricing import calc_session_cost
from smarter_dev.web.scan.tools import RateLimiter, URLRateLimiter

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()


def _usage_to_dict(usage: RunUsage) -> dict:
    """Convert RunUsage to a serializable dict, omitting zero values."""
    d = dataclasses.asdict(usage)
    return {k: v for k, v in d.items() if v}


async def _persist_aux_usage(
    session_id: UUID, usage: RunUsage, model: str = MODEL,
) -> None:
    """Persist auxiliary agent usage (meta, YouTube, code examples) to the session."""
    in_tok = usage.input_tokens or 0
    out_tok = usage.output_tokens or 0
    cache_read = usage.cache_read_tokens or 0
    cache_write = usage.cache_write_tokens or 0
    if not (in_tok or out_tok):
        return
    cost = calc_session_cost(in_tok, out_tok, cache_read, cache_write, model)
    async with get_skrift_db_session_context() as db_session:
        await ops.add_usage(
            db_session, session_id,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
        )


async def _wait_for_topic(
    session_id: UUID,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> tuple[str, str]:
    """Poll the DB for the session's topic/skill_level set by the meta task.

    Returns (topic, skill_level).  Falls back to ("other", "intermediate")
    if the meta task hasn't written context within *timeout* seconds.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        async with get_skrift_db_session_context() as db_session:
            row = await ops.get_session(db_session, session_id)
        ctx = (row.context if row else None) or {}
        if "topic" in ctx:
            return ctx["topic"], ctx.get("skill_level", "intermediate")
        await asyncio.sleep(interval)

    logger.warning("Timed out waiting for meta context on session %s", session_id)
    return "other", "intermediate"


async def _emit(user_id: str, session_id: str, event_type: str, **payload: object) -> None:
    """Emit a research notification to the user via Skrift's notification system."""
    await notify_user(
        user_id,
        f"research:{event_type}",
        mode=NotificationMode.TIMESERIES,
        session_id=session_id,
        **payload,
    )


async def run_research(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
) -> None:
    """Run the research agent as a background task.

    All progress is emitted as user-scoped notifications via Skrift's
    notification system. The result page listens for ``research:*``
    event types to render live updates.
    """
    sid = str(session_id)
    start_time = time.monotonic()

    # Build current date string in user's timezone for the system prompt
    try:
        user_tz = zoneinfo.ZoneInfo(tz) if tz else None
    except (KeyError, ValueError):
        user_tz = None
    now = datetime.now(user_tz)
    date_context = f"Today is {now.strftime('%A, %B %-d, %Y')}."

    await _emit(user_id, sid, "status", stage="planning", message="Analyzing query...")

    tool_log: list[dict] = []

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Smarter Dev Scan Agent - admin@smarter.dev"},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                search_rate_limiter=RateLimiter(min_delay=1.5),
                read_rate_limiter=URLRateLimiter(min_delay=0.0),
            )

            result_data: ResearchResult | None = None
            main_agent_usage: RunUsage | None = None

            instructions = f"{SYSTEM_PROMPT}\n\n{date_context}"

            async for event in research_agent.run_stream_events(
                query, deps=deps, model=MODEL, instructions=instructions
            ):
                if isinstance(event, FunctionToolCallEvent):
                    tool_name = event.part.tool_name
                    tool_args = event.part.args
                    await _emit(
                        user_id, sid, "tool_use",
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

                    # For research tool, include the sub-agent's tool activity
                    sub_tools: list[dict] = []
                    if tool_name == "research":
                        sub_usage_list = getattr(deps, "_sub_agent_usage", [])
                        if sub_usage_list:
                            sub_tools = sub_usage_list[-1].get("tools", [])

                    await _emit(
                        user_id, sid, "tool_result",
                        tool=tool_name,
                        status="complete",
                        content=content,
                        sub_tools=sub_tools if sub_tools else None,
                    )
                    for entry in reversed(tool_log):
                        if entry["tool"] == tool_name and entry["status"] == "running":
                            entry["status"] = "complete"
                            entry["content"] = content
                            if sub_tools:
                                entry["sub_tools"] = sub_tools
                            break

                elif isinstance(event, PartDeltaEvent):
                    if isinstance(event.delta, TextPartDelta):
                        await _emit(
                            user_id, sid, "response_chunk",
                            delta=event.delta.content_delta,
                        )

                elif isinstance(event, FinalResultEvent):
                    await _emit(
                        user_id, sid, "status",
                        stage="synthesizing",
                        message="Composing response...",
                    )

                elif isinstance(event, AgentRunResultEvent):
                    result_data = event.result.output
                    main_agent_usage = event.result.usage()

            if result_data is None:
                raise RuntimeError("Agent completed without producing a result")

            duration = time.monotonic() - start_time

            # Collect usage from main agent
            main_usage = _usage_to_dict(main_agent_usage) if main_agent_usage else {}

            # Collect sub-agent usage from deps
            sub_agent_usage = getattr(deps, "_sub_agent_usage", [])

            usage_summary = {
                "main_agent": main_usage,
                "sub_agents": sub_agent_usage,
            }
            tool_log.append({"type": "usage", **usage_summary})

            # Compute total tokens across main + sub agents
            total_in = main_usage.get("input_tokens", 0)
            total_out = main_usage.get("output_tokens", 0)
            total_cache_read = main_usage.get("cache_read_tokens", 0)
            total_cache_write = main_usage.get("cache_write_tokens", 0)
            for sub in sub_agent_usage:
                total_in += sub.get("input_tokens", 0)
                total_out += sub.get("output_tokens", 0)
                total_cache_read += sub.get("cache_read_tokens", 0)
                total_cache_write += sub.get("cache_write_tokens", 0)

            cost = calc_session_cost(
                total_in, total_out, total_cache_read, total_cache_write, MODEL,
            )

            # Persist to DB
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_result(
                    db_session,
                    session_id,
                    response=result_data.response,
                    summary=result_data.summary,
                    sources=[s.model_dump() for s in result_data.sources],
                    tool_log=tool_log,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    cache_read_tokens=total_cache_read,
                    cache_write_tokens=total_cache_write,
                    model_name=MODEL,
                    cost_usd=cost,
                )

            # Emit completion
            await _emit(
                user_id, sid, "complete",
                result_id=sid,
                result_url=f"https://scan.smarter.dev/r/{sid}",
                summary=result_data.summary,
                response=result_data.response,
                duration=round(duration, 2),
                usage=usage_summary,
            )

            # Spawn code examples task if topic is coding-related.
            # The meta task runs concurrently, so poll briefly for context.
            topic, skill_level = await _wait_for_topic(session_id)
            if topic != "other":
                task = asyncio.create_task(
                    run_code_examples(session_id, query, result_data.response, user_id, skill_level),
                    name=f"code_examples:{session_id}",
                )
                task.add_done_callback(lambda t: t.result() if not t.cancelled() and t.exception() is None else None)

    except Exception as e:
        logger.exception("Research session %s failed", sid)
        error_msg = f"{type(e).__name__}: {e}"

        try:
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, error_msg)
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)

        await _emit(user_id, sid, "error", error=error_msg, recoverable=False)


async def run_lite_research(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
) -> None:
    """Run the two-stage lite research pipeline as a background task.

    Same contract as run_research() — emits SSE events via Skrift
    notifications and persists results to the database.
    """
    sid = str(session_id)
    start_time = time.monotonic()

    # Build current date string in user's timezone for the system prompt
    try:
        user_tz = zoneinfo.ZoneInfo(tz) if tz else None
    except (KeyError, ValueError):
        user_tz = None
    now = datetime.now(user_tz)
    date_context = f"Today is {now.strftime('%A, %B %-d, %Y')}."

    async def emit(event_type: str, **payload: object) -> None:
        await _emit(user_id, sid, event_type, **payload)

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Smarter Dev Scan Agent - admin@smarter.dev"},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                search_rate_limiter=RateLimiter(min_delay=1.5),
                read_rate_limiter=URLRateLimiter(min_delay=0.0),
            )

            result_data, tool_log, total_usage = await run_lite_pipeline(
                query, deps, date_context, emit,
            )

            duration = time.monotonic() - start_time
            usage_summary = {"lite_pipeline": _usage_to_dict(total_usage)}
            tool_log.append({"type": "usage", **usage_summary})

            cache_read = total_usage.cache_read_tokens or 0
            cache_write = total_usage.cache_write_tokens or 0
            in_tokens = total_usage.input_tokens or 0
            out_tokens = total_usage.output_tokens or 0

            cost = calc_session_cost(
                in_tokens, out_tokens, cache_read, cache_write, MODEL,
            )

            # Persist to DB
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_result(
                    db_session,
                    session_id,
                    response=result_data.response,
                    summary=result_data.summary,
                    sources=[s.model_dump() for s in result_data.sources],
                    tool_log=tool_log,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    model_name=MODEL,
                    cost_usd=cost,
                )

            # Emit completion
            await _emit(
                user_id, sid, "complete",
                result_id=sid,
                result_url=f"https://scan.smarter.dev/r/{sid}",
                summary=result_data.summary,
                response=result_data.response,
                duration=round(duration, 2),
                usage=usage_summary,
            )

            # Spawn code examples task if topic is coding-related.
            # The meta task runs concurrently, so poll briefly for context.
            topic, skill_level = await _wait_for_topic(session_id)
            if topic != "other":
                task = asyncio.create_task(
                    run_code_examples(session_id, query, result_data.response, user_id, skill_level),
                    name=f"code_examples:{session_id}",
                )
                task.add_done_callback(lambda t: t.result() if not t.cancelled() and t.exception() is None else None)

    except Exception as e:
        logger.exception("Lite research session %s failed", sid)
        error_msg = f"{type(e).__name__}: {e}"

        try:
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, error_msg)
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)

        await _emit(user_id, sid, "error", error=error_msg, recoverable=False)


async def run_session_meta(
    session_id: UUID,
    query: str,
    user_id: str,
) -> None:
    """Generate session name and classification in the background.

    Emits a ``research:session_meta`` TIMESERIES notification so the result
    page can stream in the title even if it loads before this completes.
    Then fires off a YouTube video search for tech topics.
    """
    sid = str(session_id)
    try:
        meta, meta_usage = await generate_session_meta(query)
        await _persist_aux_usage(session_id, meta_usage)

        # Persist name + classification to DB
        async with get_skrift_db_session_context() as db_session:
            await ops.update_session_meta(
                db_session,
                session_id,
                name=meta.name,
                context={
                    "skill_level": meta.skill_level,
                    "topic": meta.topic,
                },
            )

        # Emit so the result page can update the title live
        await _emit(
            user_id, sid, "session_meta",
            name=meta.name,
            skill_level=meta.skill_level,
            topic=meta.topic,
        )

        # Fire off YouTube search for tech topics
        logger.info("Meta for %s: topic=%s, skill=%s — spawning YouTube search", sid, meta.topic, meta.skill_level)
        task = asyncio.create_task(
            run_youtube_search(session_id, query, user_id, meta.skill_level, meta.topic),
            name=f"youtube:{session_id}",
        )
        # Hold reference to prevent GC, log errors
        task.add_done_callback(lambda t: t.result() if not t.cancelled() and t.exception() is None else None)
    except Exception:
        logger.exception("Failed to generate session meta for %s", sid)


async def run_youtube_search(
    session_id: UUID,
    query: str,
    user_id: str,
    skill_level: str,
    topic: str,
) -> None:
    """Two-stage YouTube pipeline: search 10 results, then LLM ranks top 3.

    Stage 1: Generate search query via Flash Lite, fetch 10 results from
    the YouTube Data API.
    Stage 2: Flash Lite evaluates relevance, quality, and skill-level match,
    then selects and orders the top 1-3 videos.

    Emits a ``research:youtube_videos`` TIMESERIES notification with the
    ranked results so the sidebar can render them.
    """
    sid = str(session_id)
    try:
        yt_query, query_usage = await generate_youtube_query(query, skill_level, topic)
        await _persist_aux_usage(session_id, query_usage)
        logger.info("YouTube query for %s: %r", sid, yt_query)
        if not yt_query:
            return  # non-tech topic, skip

        async with httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "Smarter Dev Scan Agent - admin@smarter.dev"},
        ) as http_client:
            videos = await youtube_search(http_client, yt_query, num_results=10)

        if not videos or (len(videos) == 1 and "error" in videos[0]):
            logger.warning("YouTube search returned no/error results for %s: %r", sid, videos)
            return

        # Stage 2: LLM ranks and selects the most relevant videos
        logger.info("YouTube: ranking %d candidates for %s", len(videos), sid)
        ranked, rank_usage = await rank_youtube_results(query, skill_level, videos)
        await _persist_aux_usage(session_id, rank_usage)

        if not ranked:
            logger.info("YouTube: LLM selected no videos for %s", sid)
            return

        logger.info("YouTube: selected %d videos for %s", len(ranked), sid)

        # Persist ranked videos to session context
        async with get_skrift_db_session_context() as db_session:
            await ops.merge_session_context(
                db_session, session_id, {"youtube_videos": ranked},
            )

        await _emit(
            user_id, sid, "youtube_videos",
            videos=ranked,
        )
    except Exception:
        logger.exception("YouTube search failed for session %s", sid)


async def run_code_examples(
    session_id: UUID,
    query: str,
    response: str,
    user_id: str,
    skill_level: str,
) -> None:
    """Generate code examples via Flash Lite after the main response completes.

    Emits a ``research:code_examples`` TIMESERIES notification with the
    results so the result page can render them in the sidebar or content area.
    """
    sid = str(session_id)
    try:
        await _emit(user_id, sid, "code_examples_status", status="generating")
        result, code_usage = await generate_code_examples(query, response, skill_level)
        await _persist_aux_usage(session_id, code_usage, model=CODE_EXAMPLES_MODEL)

        if not result.examples:
            logger.info("No code examples generated for %s", sid)
            return

        examples_data = [ex.model_dump() for ex in result.examples]

        # Persist to session context
        async with get_skrift_db_session_context() as db_session:
            await ops.merge_session_context(
                db_session, session_id, {"code_examples": examples_data},
            )

        logger.info("Code examples: emitting %d examples for %s", len(examples_data), sid)
        await _emit(
            user_id, sid, "code_examples",
            examples=examples_data,
        )
    except Exception:
        logger.exception("Code examples generation failed for session %s", sid)


def start_meta_task(
    session_id: UUID,
    query: str,
    user_id: str,
) -> asyncio.Task:
    """Create and return an asyncio.Task for session naming/classification."""
    return asyncio.create_task(
        run_session_meta(session_id, query, user_id),
        name=f"meta:{session_id}",
    )


def start_research_task(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "lite",
    **kwargs: object,
) -> asyncio.Task:
    """Create and return an asyncio.Task for the research agent.

    Args:
        mode: "lite" (default, two-stage Flash Lite) or "premium" (multi-turn agentic).
    """
    runner = run_lite_research if mode == "lite" else run_research
    return asyncio.create_task(
        runner(session_id, query, user_id, tz=tz),
        name=f"research:{session_id}",
    )
