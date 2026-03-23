"""Background task orchestrator for research sessions.

Sequential pipeline: Meta → Research → Enrichment → Synthesis → Code Examples.

The web UI uses ``run_session_pipeline`` — a single background task that
runs all stages sequentially (except post-research enrichment which is
parallel), then writes everything to the database in one transaction.

The API keeps ``start_research_task`` for simpler research-only flows.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zoneinfo
from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic_ai.usage import RunUsage
from skrift.lib.notifications import NotificationMode, notify_user
from sqlalchemy import select, update

from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.models import ResearchSession, ScanServiceUsage, ScanUserProfile
from smarter_dev.web.scan.agent import (
    CODE_EXAMPLES_MODEL,
    MODES,
    ModeConfig,
    ResearchDeps,
    ResearchOutput,
    ResourceLink,
    _resource_sort_key,
    _usage_to_dict,
    _video_sort_key,
    generate_code_examples,
    generate_session_meta,
    generate_user_profile,
    make_slug,
    run_research,
    run_synthesis,
)
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.pricing import calc_session_cost
from smarter_dev.web.scan.tools import (
    RateLimiter,
    URLRateLimiter,
    fetch_og_metadata,
    youtube_video_details,
)

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()

_USER_AGENT = "Smarter Dev Scan Agent - admin@smarter.dev"
_MIN_VIDEO_DURATION_SECS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
            opt_out_narrative = profile.opt_out_narrative if profile else False
            opt_out_technologies = profile.opt_out_technologies if profile else False
            existing_text = profile.profile if profile else ""
            existing_techs = profile.technologies if profile else None
            recent_queries = list(profile.recent_queries or []) if profile else []
            query_count = profile.query_count if profile else 0

        # If fully opted out, only update query tracking
        if opt_out_narrative and opt_out_technologies:
            async with get_skrift_db_session_context() as db_session:
                result = await db_session.execute(
                    select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
                )
                profile = result.scalar_one_or_none()
                updated_queries = [query] + [q for q in recent_queries if q != query]
                updated_queries = updated_queries[:5]
                if profile:
                    profile.recent_queries = updated_queries
                    profile.query_count = profile.query_count + 1
                    db_session.add(profile)
                else:
                    db_session.add(ScanUserProfile(
                        user_id=user_id,
                        recent_queries=updated_queries,
                        query_count=1,
                        opt_out_narrative=True,
                        opt_out_technologies=True,
                    ))
                await db_session.commit()
            logger.info("User profile update skipped for %s (fully opted out), query tracked", user_id)
            return

        profile_output, usage = await generate_user_profile(
            query, existing_text, query_count,
            existing_technologies=existing_techs,
            recent_queries=recent_queries,
        )
        technologies = [t.model_dump() for t in profile_output.technologies]

        updated_queries = [query] + [q for q in recent_queries if q != query]
        updated_queries = updated_queries[:5]

        async with get_skrift_db_session_context() as db_session:
            result = await db_session.execute(
                select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
            )
            profile = result.scalar_one_or_none()
            suggested = profile_output.suggested_queries[:3] if profile_output.suggested_queries else None
            if profile:
                if not opt_out_narrative:
                    profile.profile = profile_output.profile
                    profile.suggested_queries = suggested
                if not opt_out_technologies:
                    profile.technologies = technologies
                profile.recent_queries = updated_queries
                profile.query_count = profile.query_count + 1
                db_session.add(profile)
            else:
                db_session.add(ScanUserProfile(
                    user_id=user_id,
                    profile="" if opt_out_narrative else profile_output.profile,
                    technologies=None if opt_out_technologies else technologies,
                    recent_queries=updated_queries,
                    suggested_queries=None if opt_out_narrative else suggested,
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


# ---------------------------------------------------------------------------
# Post-research enrichment
# ---------------------------------------------------------------------------


async def _enrich_youtube(
    research_output: ResearchOutput,
    http_client: httpx.AsyncClient,
    emit,
) -> list[dict]:
    """Fetch YouTube metadata for videos found by the researcher.

    Filters out short videos (<5min), sorts by quality tier.
    """
    if not research_output.youtube_urls:
        logger.info("YouTube enrichment: no youtube_urls in research output")
        return []

    try:
        # Extract video IDs from URLs
        video_ids = []
        for yt in research_output.youtube_urls:
            logger.info("YouTube enrichment: processing URL %s", yt.url)
            parsed = urlparse(yt.url)
            if "youtube.com" in parsed.netloc:
                from urllib.parse import parse_qs
                qs = parse_qs(parsed.query)
                vid = qs.get("v", [None])[0]
                if vid:
                    video_ids.append(vid)
            elif "youtu.be" in parsed.netloc:
                vid = parsed.path.lstrip("/")
                if vid:
                    video_ids.append(vid)

        logger.info("YouTube enrichment: extracted %d video IDs from %d URLs", len(video_ids), len(research_output.youtube_urls))
        if not video_ids:
            return []

        videos = await youtube_video_details(http_client, video_ids)
        if not videos:
            return []

        # Filter by duration (>= 5 minutes)
        filtered = []
        for v in videos:
            duration = v.get("duration_seconds", 0)
            if duration >= _MIN_VIDEO_DURATION_SECS:
                filtered.append(v)

        # Sort by quality tier
        filtered.sort(key=_video_sort_key)

        # Take top 4
        result = filtered[:4]

        if result:
            await emit("youtube_videos", videos=result)

        return result
    except Exception:
        logger.exception("YouTube enrichment failed")
        return []


async def _enrich_resources(
    research_output: ResearchOutput,
    http_client: httpx.AsyncClient,
    emit,
) -> list[dict]:
    """Fetch OG metadata for resource links found by the researcher.

    Sorts by domain quality tier.
    """
    if not research_output.resources:
        return []

    try:
        # Fetch OG metadata in parallel
        async def _fetch_one(resource: ResourceLink) -> dict:
            og = await fetch_og_metadata(http_client, resource.url)
            base = {
                "title": resource.title,
                "url": resource.url,
                "description": resource.description,
            }
            if og:
                base["og_image"] = og.get("og_image", "")
                base["site_name"] = og.get("og_site_name") or og.get("site_name") or urlparse(resource.url).netloc
                base["favicon"] = og.get("favicon", "")
                if og.get("og_title"):
                    base["title"] = og["og_title"]
                if og.get("og_description") and not base["description"]:
                    base["description"] = og["og_description"]
            else:
                base["site_name"] = urlparse(resource.url).netloc
                base["og_image"] = ""
                base["favicon"] = ""
            return base

        tasks = [_fetch_one(r) for r in research_output.resources]
        enriched = await asyncio.gather(*tasks, return_exceptions=True)

        result = [r for r in enriched if isinstance(r, dict)]
        result.sort(key=_resource_sort_key)

        # Take top 5
        result = result[:5]

        if result:
            await emit("resources", resources=result)

        return result
    except Exception:
        logger.exception("Resource enrichment failed")
        return []


# ---------------------------------------------------------------------------
# Unified web pipeline — sequential stages, single DB write
# ---------------------------------------------------------------------------


async def run_session_pipeline(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "auto",
) -> None:
    """Sequential pipeline orchestrator for the web UI.

    Stages: Meta → Research → Enrichment → Synthesis → Code Examples → Persist.
    """
    sid = str(session_id)
    start_time = time.monotonic()
    date_context = _build_date_context(tz)

    # Mutable containers for results
    all_usage: list[tuple[RunUsage, str]] = []  # (usage, model_name)
    youtube_videos: list[dict] = []
    resources: list[dict] = []
    code_examples_data: list[dict] = []

    # Inner emit helper pre-fills user_id and session_id
    async def emit(event_type: str, **payload: object) -> None:
        await _emit(user_id, sid, event_type, **payload)

    try:
        # -- Fetch user profile --
        user_profile_text = ""
        async with get_skrift_db_session_context() as db_session:
            result = await db_session.execute(
                select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
            )
            profile_row = result.scalar_one_or_none()
            if profile_row:
                parts = []
                if profile_row.profile and not profile_row.opt_out_narrative:
                    parts.append(profile_row.profile)
                if profile_row.technologies and not profile_row.opt_out_technologies:
                    tech_str = ", ".join(
                        f"{t['name']} ({t['relationship']})"
                        for t in profile_row.technologies
                    )
                    parts.append(f"Technologies: {tech_str}")
                if profile_row.recent_queries:
                    recent = profile_row.recent_queries[:5]
                    parts.append("Recent queries: " + "; ".join(recent))
                user_profile_text = "\n\n".join(parts)

        # -- Stage 0: Meta analysis + mode detection --
        meta, meta_usage = await generate_session_meta(query)
        all_usage.append((meta_usage, "google-gla:gemini-3.1-flash-lite-preview"))

        # Override mode if explicitly requested
        research_mode = meta.research_mode
        if mode != "auto" and mode in MODES:
            research_mode = mode

        mode_config = MODES[research_mode]
        session_slug = make_slug(meta.name)

        await emit(
            "session_meta",
            name=meta.name,
            slug=session_slug,
            skill_level=meta.skill_level,
            topic=meta.topic,
        )

        # -- Stage 1: Research --
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                search_rate_limiter=RateLimiter(min_delay=1.0),
                read_rate_limiter=URLRateLimiter(min_delay=0.0),
            )

            research_output, tool_log, research_usage = await run_research(
                query, deps, mode_config, date_context, emit,
                user_profile=user_profile_text,
            )
            all_usage.append((research_usage, mode_config.research_model))

            # -- Stage 2: Post-research enrichment (parallel) --
            yt_task = _enrich_youtube(research_output, http_client, emit)
            res_task = _enrich_resources(research_output, http_client, emit)
            yt_result, res_result = await asyncio.gather(yt_task, res_task, return_exceptions=True)

            if isinstance(yt_result, list):
                youtube_videos = yt_result
            if isinstance(res_result, list):
                resources = res_result

        # -- Stage 3: Synthesis (clean context) --
        synthesis_result, synthesis_usage = await run_synthesis(
            query, research_output, mode_config,
            user_profile=user_profile_text,
            emit=emit,
        )
        all_usage.append((synthesis_usage, mode_config.synthesis_model))

        # Emit complete event
        duration = time.monotonic() - start_time
        result_url = f"https://scan.smarter.dev/r/{session_slug}"
        await emit(
            "complete",
            result_id=sid,
            result_url=result_url,
            summary=synthesis_result.summary,
            response=synthesis_result.response,
            sources=[s.model_dump() for s in synthesis_result.sources],
            research_mode=research_mode,
            duration=round(duration, 2),
        )

        # -- Stage 4: Code examples (sequential, post-synthesis) --
        if meta.topic != "other":
            try:
                await emit("code_examples_status", status="generating")
                examples_result, examples_usage = await generate_code_examples(
                    query, synthesis_result.response, meta.skill_level,
                )
                all_usage.append((examples_usage, CODE_EXAMPLES_MODEL))
                code_examples_data = [ex.model_dump() for ex in examples_result.examples]
                if code_examples_data:
                    await emit("code_examples", examples=code_examples_data)
            except Exception:
                logger.exception("Code examples generation failed")
            finally:
                await emit("code_examples_status", status="done")

        # -- Aggregate usage --
        total_in = sum(u.input_tokens or 0 for u, _ in all_usage)
        total_out = sum(u.output_tokens or 0 for u, _ in all_usage)
        total_cache_read = sum(u.cache_read_tokens or 0 for u, _ in all_usage)
        total_cache_write = sum(u.cache_write_tokens or 0 for u, _ in all_usage)

        # Calculate cost using the primary research model for pricing
        cost = calc_session_cost(
            total_in, total_out, total_cache_read, total_cache_write,
            mode_config.research_model,
        )

        # -- Build context dict --
        context: dict = {}
        if meta.query_format != "simple":
            context["query_format"] = meta.query_format
        if meta.topic != "other":
            context["topic"] = meta.topic
            context["skill_level"] = meta.skill_level
        if youtube_videos:
            context["youtube_videos"] = youtube_videos
        if resources:
            context["resources"] = resources
        if code_examples_data:
            context["code_examples"] = code_examples_data
        if user_profile_text:
            context["user_profile_snapshot"] = user_profile_text

        # -- Single DB write --
        async with get_skrift_db_session_context() as db_session:
            values: dict = {
                "status": "complete",
                "response": synthesis_result.response,
                "summary": synthesis_result.summary,
                "sources": [s.model_dump() for s in synthesis_result.sources],
                "tool_log": tool_log,
                "context": context,
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
                "model_name": mode_config.research_model,
                "cost_usd": cost,
                "name": meta.name,
                "slug": session_slug,
                "pipeline_mode": research_mode,
            }
            await db_session.execute(
                update(ResearchSession)
                .where(ResearchSession.id == session_id)
                .values(**values)
            )
            await db_session.commit()

        # Background profile update
        asyncio.create_task(
            _update_user_profile(user_id, query, session_id=session_id),
            name=f"profile:{sid}",
        )

    except Exception as exc:
        logger.exception("Research pipeline failed for session %s", sid)
        try:
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, str(exc))
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)
        await emit("error", error=str(exc), recoverable=False)


def start_pipeline_task(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "auto",
) -> asyncio.Task:
    """Start the unified web pipeline as a single background task."""
    task = asyncio.create_task(
        run_session_pipeline(session_id, query, user_id, tz=tz, mode=mode),
        name=f"scan:{session_id}",
    )

    def _done_cb(t: asyncio.Task) -> None:
        if t.cancelled():
            logger.warning("Pipeline task cancelled for %s", session_id)
        elif exc := t.exception():
            logger.error("Pipeline task failed for %s: %s", session_id, exc)

    task.add_done_callback(_done_cb)
    return task


# ---------------------------------------------------------------------------
# API path — simplified lite research (no meta/sidebar tasks)
# ---------------------------------------------------------------------------


async def run_lite_research(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
) -> None:
    """Run a simplified research pipeline for API clients."""
    sid = str(session_id)
    start_time = time.monotonic()
    date_context = _build_date_context(tz)

    async def emit(event_type: str, **payload: object) -> None:
        await _emit(user_id, sid, event_type, **payload)

    try:
        mode_config = MODES["quick_answer"]

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={"User-Agent": _USER_AGENT},
        ) as http_client:
            deps = ResearchDeps(
                session_id=sid,
                http_client=http_client,
                search_rate_limiter=RateLimiter(min_delay=1.0),
                read_rate_limiter=URLRateLimiter(min_delay=0.0),
            )

            research_output, tool_log, research_usage = await run_research(
                query, deps, mode_config, date_context, emit,
            )

        synthesis_result, synthesis_usage = await run_synthesis(
            query, research_output, mode_config, emit=emit,
        )

        total_usage = RunUsage(
            input_tokens=(research_usage.input_tokens or 0) + (synthesis_usage.input_tokens or 0),
            output_tokens=(research_usage.output_tokens or 0) + (synthesis_usage.output_tokens or 0),
            cache_read_tokens=(research_usage.cache_read_tokens or 0) + (synthesis_usage.cache_read_tokens or 0),
            cache_write_tokens=(research_usage.cache_write_tokens or 0) + (synthesis_usage.cache_write_tokens or 0),
        )

        duration = time.monotonic() - start_time
        usage_summary = {"pipeline": _usage_to_dict(total_usage)}
        tool_log.append({"type": "usage", **usage_summary})

        cost = calc_session_cost(
            total_usage.input_tokens or 0,
            total_usage.output_tokens or 0,
            total_usage.cache_read_tokens or 0,
            total_usage.cache_write_tokens or 0,
            mode_config.research_model,
        )

        async with get_skrift_db_session_context() as db_session:
            await ops.update_session_result(
                db_session, session_id,
                response=synthesis_result.response,
                summary=synthesis_result.summary,
                sources=[s.model_dump() for s in synthesis_result.sources],
                tool_log=tool_log,
                input_tokens=total_usage.input_tokens or 0,
                output_tokens=total_usage.output_tokens or 0,
                cache_read_tokens=total_usage.cache_read_tokens or 0,
                cache_write_tokens=total_usage.cache_write_tokens or 0,
                model_name=mode_config.research_model,
                cost_usd=cost,
            )

        result_url = f"https://scan.smarter.dev/r/{session_id}"
        await emit(
            "complete",
            result_id=sid,
            result_url=result_url,
            summary=synthesis_result.summary,
            response=synthesis_result.response,
            sources=[s.model_dump() for s in synthesis_result.sources],
            duration=round(duration, 2),
            usage=usage_summary,
        )

    except Exception as exc:
        logger.exception("API research failed for session %s", sid)
        try:
            async with get_skrift_db_session_context() as db_session:
                await ops.update_session_error(db_session, session_id, str(exc))
        except Exception:
            logger.exception("Failed to persist error for session %s", sid)
        await emit("error", error=str(exc), recoverable=False)


def start_research_task(
    session_id: UUID,
    query: str,
    user_id: str,
    tz: str | None = None,
    mode: str = "auto",
    **kwargs: object,
) -> asyncio.Task:
    """Start a research-only task (API path — no meta/sidebar)."""
    runner = run_lite_research
    return asyncio.create_task(
        runner(session_id, query, user_id, tz=tz),
        name=f"scan-api:{session_id}",
    )
