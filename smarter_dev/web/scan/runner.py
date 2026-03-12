"""Background task orchestrator for research sessions.

Runs the Pydantic AI agent, emits user-scoped notifications via Skrift's
notification system, and persists results to the database.

The web UI uses ``run_session_pipeline`` — a single background task that
runs meta, research, YouTube, resources, and code-examples concurrently,
then writes everything to the database in one transaction.

The API keeps ``start_research_task`` for simpler research-only flows.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import zoneinfo
from datetime import datetime
from urllib.parse import urlparse
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
from sqlalchemy import select, update

from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.models import ResearchSession, ScanServiceUsage, ScanUserProfile
from smarter_dev.web.scan.agent import (
    CODE_EXAMPLES_MODEL,
    MODEL,
    SYSTEM_PROMPT,
    ResearchDeps,
    ResearchResult,
    generate_code_examples,
    generate_session_meta,
    generate_user_profile,
    generate_youtube_query,
    make_slug,
    rank_resource_results,
    rank_youtube_results,
    research_agent,
    run_experimental_pipeline,
    run_lite_pipeline,
)
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.pricing import calc_session_cost
from smarter_dev.web.scan.tools import (
    RateLimiter,
    URLRateLimiter,
    _duration_to_seconds,
    brave_search,
    fetch_og_metadata,
    youtube_search,
    youtube_video_details,
)

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()

_USER_AGENT = "Smarter Dev Scan Agent - admin@smarter.dev"




def _usage_to_dict(usage: RunUsage) -> dict:
    """Convert RunUsage to a serializable dict, omitting zero values."""
    d = dataclasses.asdict(usage)
    return {k: v for k, v in d.items() if v}


async def _emit(user_id: str, session_id: str, event_type: str, **payload: object) -> None:
    """Emit a research notification to the user via Skrift's notification system."""
    await notify_user(
        user_id,
        f"research:{event_type}",
        mode=NotificationMode.TIMESERIES,
        session_id=session_id,
        **payload,
    )


def _build_date_context(tz: str | None) -> str:
    """Build a date-context string in the user's timezone."""
    try:
        user_tz = zoneinfo.ZoneInfo(tz) if tz else None
    except (KeyError, ValueError):
        user_tz = None
    now = datetime.now(user_tz)
    return f"Today is {now.strftime('%A, %B %-d, %Y')}."


async def _update_user_profile(user_id: str, query: str, session_id: UUID | None = None) -> None:
    """Background task: update the user's Scan profile based on their query."""
    try:
        async with get_skrift_db_session_context() as db_session:
            result = await db_session.execute(
                select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()
            existing_text = profile.profile if profile else ""
            existing_techs = profile.technologies if profile else None
            recent_queries = list(profile.recent_queries or []) if profile else []
            query_count = profile.query_count if profile else 0

        profile_output, usage = await generate_user_profile(
            query, existing_text, query_count,
            existing_technologies=existing_techs,
            recent_queries=recent_queries,
        )
        technologies = [t.model_dump() for t in profile_output.technologies]

        # Keep last 5 queries (most recent first)
        updated_queries = [query] + [q for q in recent_queries if q != query]
        updated_queries = updated_queries[:5]

        async with get_skrift_db_session_context() as db_session:
            result = await db_session.execute(
                select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()
            suggested = profile_output.suggested_queries[:3] if profile_output.suggested_queries else None
            if profile:
                profile.profile = profile_output.profile
                profile.technologies = technologies
                profile.recent_queries = updated_queries
                profile.suggested_queries = suggested
                profile.query_count = profile.query_count + 1
                db_session.add(profile)
            else:
                db_session.add(ScanUserProfile(
                    user_id=user_id,
                    profile=profile_output.profile,
                    technologies=technologies,
                    recent_queries=updated_queries,
                    suggested_queries=suggested,
                    query_count=1,
                ))
            await db_session.commit()

        # Track profiler usage separately as an internal service cost
        if usage and (usage.input_tokens or usage.output_tokens):
            in_tok = usage.input_tokens or 0
            out_tok = usage.output_tokens or 0
            cache_read = usage.cache_read_tokens or 0
            cache_write = usage.cache_write_tokens or 0
            cost = calc_session_cost(in_tok, out_tok, cache_read, cache_write, CODE_EXAMPLES_MODEL)
            async with get_skrift_db_session_context() as db_session:
                db_session.add(ScanServiceUsage(
                    task_type="user_profiler",
                    model_name=CODE_EXAMPLES_MODEL,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    cost_usd=cost,
                    user_id=user_id,
                    session_id=session_id,
                ))
                await db_session.commit()

        logger.info("User profile updated for %s", user_id)
    except Exception:
        logger.exception("Failed to update user profile for %s", user_id)


# ============================================================================
# Unified web pipeline — single task, single DB write
# ============================================================================


async def run_session_pipeline(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "lite",
) -> None:
    """Single pipeline orchestrator for the web UI.

    Runs all phases concurrently (meta, research, YouTube, resources,
    code-examples), emits SSE events for live updates, and persists
    everything to the database in one transaction at the end.
    """
    sid = str(session_id)
    start_time = time.monotonic()
    date_context = _build_date_context(tz)

    # -- Fetch existing user profile for planner context --
    user_profile_text: str = ""
    try:
        async with get_skrift_db_session_context() as db_session:
            profile_result = await db_session.execute(
                select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
            )
            profile_row = profile_result.scalar_one_or_none()
            if profile_row and profile_row.profile:
                parts = [profile_row.profile]
                if profile_row.technologies:
                    tech_lines = []
                    for t in profile_row.technologies:
                        tech_lines.append(f"- {t['name']} ({t['relationship']})")
                    parts.append("Technologies:\n" + "\n".join(tech_lines))
                if profile_row.recent_queries:
                    rq_lines = [f"{i}. {q}" for i, q in enumerate(profile_row.recent_queries, 1)]
                    parts.append("Recent searches:\n" + "\n".join(rq_lines))
                user_profile_text = "\n\n".join(parts)
    except Exception:
        logger.warning("Failed to fetch user profile for %s, continuing without it", user_id)

    # -- Shared mutable state filled by phases --
    meta_ready = asyncio.Event()
    research_done = asyncio.Event()
    planner_reasoning: str = ""

    # Results collected in memory — written to DB once at the end.
    meta_name: str | None = None
    session_slug: str | None = None
    meta_topic: str = "other"
    meta_skill: str = "intermediate"
    research_result: ResearchResult | None = None
    research_tool_log: list[dict] = []
    youtube_videos: list[dict] = []
    resources: list[dict] = []
    code_examples_data: list[dict] = []
    planner_output_data: dict | None = None
    example_plan_data: list[dict] = []
    all_usage: list[tuple[RunUsage, str]] = []  # (usage, model_name)

    async def emit(event_type: str, **payload: object) -> None:
        await _emit(user_id, sid, event_type, **payload)

    # -- Phase functions (do work + emit SSE, no DB) --

    async def _do_meta() -> None:
        nonlocal meta_name, meta_topic, meta_skill, session_slug
        meta, usage = await generate_session_meta(query)
        meta_name = meta.name
        session_slug = make_slug(meta.name) if meta.name else None
        meta_topic = meta.topic
        meta_skill = meta.skill_level
        all_usage.append((usage, MODEL))
        await emit("session_meta", name=meta.name, slug=session_slug, skill_level=meta.skill_level, topic=meta.topic)
        meta_ready.set()

    async def _do_lite_research() -> None:
        nonlocal research_result, research_tool_log
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": _USER_AGENT},
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
        research_result = result_data
        research_tool_log = tool_log
        all_usage.append((total_usage, MODEL))

        duration = time.monotonic() - start_time
        result_path = session_slug or sid
        await emit(
            "complete",
            result_id=sid,
            result_url=f"https://scan.smarter.dev/r/{result_path}",
            summary=result_data.summary,
            response=result_data.response,
            sources=[s.model_dump() for s in result_data.sources],
            duration=round(duration, 2),
        )
        research_done.set()

    async def _do_premium_research() -> None:
        nonlocal research_result, research_tool_log
        tool_log: list[dict] = []

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                search_rate_limiter=RateLimiter(min_delay=1.5),
                read_rate_limiter=URLRateLimiter(min_delay=0.0),
            )
            await emit("status", stage="planning", message="Analyzing query...")
            instructions = f"{SYSTEM_PROMPT}\n\n{date_context}"

            result_data: ResearchResult | None = None
            main_agent_usage: RunUsage | None = None

            async for event in research_agent.run_stream_events(
                query, deps=deps, model=MODEL, instructions=instructions,
            ):
                if isinstance(event, FunctionToolCallEvent):
                    tool_name = event.part.tool_name
                    tool_args = event.part.args
                    await emit(
                        "tool_use",
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
                    sub_tools: list[dict] = []
                    if tool_name == "research":
                        sub_usage_list = getattr(deps, "_sub_agent_usage", [])
                        if sub_usage_list:
                            sub_tools = sub_usage_list[-1].get("tools", [])
                    await emit(
                        "tool_result", tool=tool_name, status="complete",
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
                        await emit("response_chunk", delta=event.delta.content_delta)
                elif isinstance(event, FinalResultEvent):
                    await emit("status", stage="synthesizing", message="Composing response...")
                elif isinstance(event, AgentRunResultEvent):
                    result_data = event.result.output
                    main_agent_usage = event.result.usage()

            if result_data is None:
                raise RuntimeError("Agent completed without producing a result")

            # Collect main + sub-agent usage
            main_usage = _usage_to_dict(main_agent_usage) if main_agent_usage else {}
            sub_agent_usage = getattr(deps, "_sub_agent_usage", [])
            combined = RunUsage(
                input_tokens=main_usage.get("input_tokens", 0) + sum(s.get("input_tokens", 0) for s in sub_agent_usage),
                output_tokens=main_usage.get("output_tokens", 0) + sum(s.get("output_tokens", 0) for s in sub_agent_usage),
                cache_read_tokens=main_usage.get("cache_read_tokens", 0) + sum(s.get("cache_read_tokens", 0) for s in sub_agent_usage),
                cache_write_tokens=main_usage.get("cache_write_tokens", 0) + sum(s.get("cache_write_tokens", 0) for s in sub_agent_usage),
            )

        research_result = result_data
        research_tool_log = tool_log
        all_usage.append((combined, MODEL))

        duration = time.monotonic() - start_time
        result_path = session_slug or sid
        await emit(
            "complete",
            result_id=sid,
            result_url=f"https://scan.smarter.dev/r/{result_path}",
            summary=result_data.summary,
            response=result_data.response,
            sources=[s.model_dump() for s in result_data.sources],
            duration=round(duration, 2),
        )
        research_done.set()

    async def _do_youtube() -> None:
        nonlocal youtube_videos
        await meta_ready.wait()
        if meta_topic == "other":
            return

        yt_query, query_usage = await generate_youtube_query(query, meta_skill, meta_topic)
        all_usage.append((query_usage, MODEL))
        logger.info("YouTube query for %s: %r", sid, yt_query)
        if not yt_query:
            return

        async with httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            videos = await youtube_search(http_client, yt_query, num_results=20)

        if not videos or (len(videos) == 1 and "error" in videos[0]):
            logger.warning("YouTube search returned no/error results for %s", sid)
            return

        # Fetch full metadata (including duration) before ranking so we can
        # filter out shorts and videos under 5 minutes.
        video_ids = [v.get("video_id", "") for v in videos if v.get("video_id")]
        if video_ids:
            async with httpx.AsyncClient(
                timeout=15.0, headers={"User-Agent": _USER_AGENT},
            ) as yt_client:
                detailed = await youtube_video_details(yt_client, video_ids)
            if detailed:
                videos = detailed

        # Filter out videos shorter than 5 minutes
        min_duration_secs = 5 * 60
        videos = [
            v for v in videos
            if _duration_to_seconds(v.get("duration", "")) >= min_duration_secs
        ]

        if not videos:
            logger.warning("YouTube: no videos >= 5 min for %s", sid)
            return

        logger.info("YouTube: ranking %d candidates for %s", len(videos), sid)
        ranked, rank_usage = await rank_youtube_results(query, meta_skill, videos)
        all_usage.append((rank_usage, MODEL))

        if ranked:
            youtube_videos = ranked
            logger.info("YouTube: selected %d videos for %s", len(ranked), sid)
            await emit("youtube_videos", videos=ranked)

    async def _do_resources() -> None:
        nonlocal resources
        await meta_ready.wait()
        if meta_topic == "other":
            return

        async with httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            results = await brave_search(
                http_client, f"{query} tutorial guide documentation", num_results=15,
            )

        if not results or (len(results) == 1 and "error" in results[0]):
            logger.warning("Brave resource search returned no results for %s", sid)
            return

        ranked, rank_usage = await rank_resource_results(query, meta_skill, results)
        all_usage.append((rank_usage, MODEL))
        if not ranked:
            return

        logger.info("Resources: selected %d, fetching OG metadata for %s", len(ranked), sid)

        async with httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            og_tasks = [fetch_og_metadata(http_client, r["url"]) for r in ranked]
            og_results = await asyncio.gather(*og_tasks, return_exceptions=True)

        resource_list: list[dict] = []
        for r, og in zip(ranked, og_results):
            resource: dict = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
            }
            if isinstance(og, dict):
                if og.get("og_title"):
                    resource["title"] = og["og_title"]
                if og.get("og_description"):
                    resource["description"] = og["og_description"]
                if og.get("og_image"):
                    resource["og_image"] = og["og_image"]
                if og.get("og_site_name"):
                    resource["site_name"] = og["og_site_name"]
                if og.get("favicon"):
                    resource["favicon"] = og["favicon"]
            if "site_name" not in resource:
                resource["site_name"] = urlparse(r.get("url", "")).netloc.replace("www.", "")
            resource_list.append(resource)

        resources = resource_list
        logger.info("Resources: emitting %d resources for %s", len(resources), sid)
        await emit("resources", resources=resources)

    async def _do_code_examples() -> None:
        nonlocal code_examples_data
        await meta_ready.wait()
        await research_done.wait()
        if meta_topic == "other" or not research_result:
            return

        try:
            await emit("code_examples_status", status="generating")
            result, usage = await generate_code_examples(
                query, research_result.response, meta_skill,
            )
            all_usage.append((usage, CODE_EXAMPLES_MODEL))

            if result.examples:
                code_examples_data = [ex.model_dump() for ex in result.examples]
                logger.info("Code examples: emitting %d examples for %s", len(code_examples_data), sid)
                await emit("code_examples", examples=code_examples_data)
        except Exception as exc:
            logger.exception("Code examples failed: %s: %s", type(exc).__name__, exc)
        finally:
            await emit("code_examples_status", status="done")

    async def _do_experimental_research() -> None:
        nonlocal meta_name, meta_topic, meta_skill, session_slug
        nonlocal research_result, research_tool_log
        nonlocal youtube_videos, resources, code_examples_data
        nonlocal planner_reasoning, planner_output_data, example_plan_data

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                search_rate_limiter=RateLimiter(min_delay=1.5),
                read_rate_limiter=URLRateLimiter(min_delay=0.0),
            )
            (
                result_data, tool_log, total_usage,
                exp_youtube, exp_resources, exp_examples, meta_plan,
                exp_planner_reasoning, exp_slug, exp_planner_output,
                exp_example_plan,
            ) = await run_experimental_pipeline(
                query, deps, date_context, emit,
                user_profile=user_profile_text,
            )

        research_result = result_data
        research_tool_log = tool_log
        planner_reasoning = exp_planner_reasoning
        planner_output_data = exp_planner_output
        example_plan_data = exp_example_plan
        all_usage.append((total_usage, MODEL))

        # Capture meta from the experimental pipeline's combined output
        meta_name = meta_plan.name
        session_slug = exp_slug or None
        meta_topic = meta_plan.topic
        meta_skill = meta_plan.skill_level
        youtube_videos = exp_youtube
        resources = exp_resources
        code_examples_data = exp_examples

        duration = time.monotonic() - start_time
        result_path = session_slug or sid
        await emit(
            "complete",
            result_id=sid,
            result_url=f"https://scan.smarter.dev/r/{result_path}",
            summary=result_data.summary,
            response=result_data.response,
            sources=[s.model_dump() for s in result_data.sources],
            duration=round(duration, 2),
        )
        research_done.set()

    # -- Run all phases concurrently --
    try:
        if mode == "experimental":
            # Experimental pipeline handles everything sequentially
            results = await asyncio.gather(
                _do_experimental_research(),
                return_exceptions=True,
            )
            phase_names = ["experimental"]
        else:
            research_fn = _do_lite_research if mode == "lite" else _do_premium_research

            results = await asyncio.gather(
                _do_meta(),
                research_fn(),
                _do_youtube(),
                _do_resources(),
                _do_code_examples(),
                return_exceptions=True,
            )
            phase_names = ["meta", "research", "youtube", "resources", "code_examples"]

        # Log any phase failures
        for name, result in zip(phase_names, results):
            if isinstance(result, Exception):
                logger.error(
                    "Pipeline phase '%s' failed for %s: %s",
                    name, sid, result, exc_info=result,
                )

        # Check if research itself failed
        research_idx = 0 if mode == "experimental" else 1
        if isinstance(results[research_idx], Exception):
            raise results[research_idx]

        if research_result is None:
            raise RuntimeError("Research pipeline completed without producing a result")

        # -- Aggregate usage --
        total_in = sum(u.input_tokens or 0 for u, _ in all_usage)
        total_out = sum(u.output_tokens or 0 for u, _ in all_usage)
        total_cache_read = sum(u.cache_read_tokens or 0 for u, _ in all_usage)
        total_cache_write = sum(u.cache_write_tokens or 0 for u, _ in all_usage)
        cost = calc_session_cost(total_in, total_out, total_cache_read, total_cache_write, MODEL)

        # -- Build context --
        context: dict = {}
        if meta_topic != "other":
            context["topic"] = meta_topic
            context["skill_level"] = meta_skill
        if youtube_videos:
            context["youtube_videos"] = youtube_videos
        if resources:
            context["resources"] = resources
        if code_examples_data:
            context["code_examples"] = code_examples_data
        if planner_reasoning:
            context["planner_reasoning"] = planner_reasoning
        if planner_output_data:
            context["planner_output"] = planner_output_data
        if example_plan_data:
            context["example_plan"] = example_plan_data
        if user_profile_text:
            context["user_profile_snapshot"] = user_profile_text

        # -- Single DB write --
        async with get_skrift_db_session_context() as db_session:
            values: dict = {
                "status": "complete",
                "response": research_result.response,
                "summary": research_result.summary,
                "sources": [s.model_dump() for s in research_result.sources],
                "tool_log": research_tool_log,
                "context": context,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
                "model_name": MODEL,
                "cost_usd": cost,
            }
            if meta_name:
                values["name"] = meta_name
            if session_slug:
                values["slug"] = session_slug
            await db_session.execute(
                update(ResearchSession)
                .where(ResearchSession.id == session_id)
                .values(**values)
            )
            await db_session.commit()

        logger.info("Pipeline complete for %s — persisted in single DB write", sid)

        # Fire-and-forget: update user profile in the background
        asyncio.create_task(
            _update_user_profile(user_id, query, session_id=session_id),
            name=f"profile:{sid}",
        )

    except Exception as e:
        logger.exception("Research pipeline %s failed", sid)
        error_msg = f"{type(e).__name__}: {e}"
        try:
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, error_msg)
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)
        await _emit(user_id, sid, "error", error=error_msg, recoverable=False)


def start_pipeline_task(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "lite",
) -> asyncio.Task:
    """Start the unified web pipeline as a single background task."""
    task = asyncio.create_task(
        run_session_pipeline(session_id, query, user_id, tz=tz, mode=mode),
        name=f"pipeline:{session_id}",
    )
    task.add_done_callback(
        lambda t: t.result() if not t.cancelled() and t.exception() is None else None
    )
    return task


# ============================================================================
# API-only task starters (used by the FastAPI research router)
# ============================================================================


async def _persist_aux_usage(
    session_id: UUID, usage: RunUsage, model: str = MODEL,
) -> None:
    """Persist auxiliary agent usage to the session (API path only)."""
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


async def run_lite_research(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
) -> None:
    """Run the lite research pipeline (API path — no meta/sidebar tasks)."""
    sid = str(session_id)
    start_time = time.monotonic()
    date_context = _build_date_context(tz)

    async def emit(event_type: str, **payload: object) -> None:
        await _emit(user_id, sid, event_type, **payload)

    try:
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": _USER_AGENT},
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

            in_tokens = total_usage.input_tokens or 0
            out_tokens = total_usage.output_tokens or 0
            cache_read = total_usage.cache_read_tokens or 0
            cache_write = total_usage.cache_write_tokens or 0

            cost = calc_session_cost(in_tokens, out_tokens, cache_read, cache_write, MODEL)

            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_result(
                    db_session, session_id,
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

            await _emit(
                user_id, sid, "complete",
                result_id=sid,
                result_url=f"https://scan.smarter.dev/r/{sid}",
                summary=result_data.summary,
                response=result_data.response,
                sources=[s.model_dump() for s in result_data.sources],
                duration=round(duration, 2),
                usage=usage_summary,
            )

    except Exception as e:
        logger.exception("Lite research session %s failed", sid)
        error_msg = f"{type(e).__name__}: {e}"
        try:
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, error_msg)
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)
        await _emit(user_id, sid, "error", error=error_msg, recoverable=False)


def start_research_task(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "lite",
    **kwargs: object,
) -> asyncio.Task:
    """Start a research-only task (API path — no meta/sidebar).

    Args:
        mode: "lite" (default) or "premium".
    """
    runner = run_lite_research  # premium API path not currently used
    return asyncio.create_task(
        runner(session_id, query, user_id, tz=tz),
        name=f"research:{session_id}",
    )
