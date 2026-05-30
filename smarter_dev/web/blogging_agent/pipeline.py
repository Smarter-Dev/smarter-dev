"""Authoring pipeline orchestrator — Skrift worker handler.

Submits five Skrift Agent runs in sequence inside one worker job:
Review → Scout → Brainstorm → Research → Synthesis. Each stage's
session_id is recorded onto the ``authoring_pipeline_runs`` row so the
admin UI can replay/subscribe to Skrift's native event log for the audit
view.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from skrift.workers import handler
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
from smarter_dev.web.blogging_agent.brainstorm_agent import (
    BrainstormCandidate,
    BrainstormInput,
    BrainstormOutput,
    brainstorm_agent,
    build_brainstorm_user_turn,
)
from smarter_dev.web.blogging_agent.cache import drop_cache, register_cache
from smarter_dev.web.blogging_agent.research_agent import (
    ResearchInput,
    ResearchOutput,
    build_research_user_turn,
    research_agent,
)
from smarter_dev.web.blogging_agent.review_agent import (
    CandidateTopicView,
    ReviewInput,
    ReviewOutput,
    build_review_user_turn,
    review_agent,
)
from smarter_dev.web.blogging_agent.scout_agent import (
    ScoutOutput,
    scout_agent,
)
from smarter_dev.web.blogging_agent.synthesis_agent import (
    SynthesisInput,
    SynthesisOutput,
    build_synthesis_user_turn,
    synthesis_agent,
)
from smarter_dev.web.models import (
    AuthoringPipelineRun,
    BlogPostMeta,
    CandidateBlogTopic,
)

logger = logging.getLogger(__name__)

SMARTER_DEV_AGENT_EMAIL = "agent@smarter.dev"


class PipelineRunPayload(BaseModel):
    """Worker job payload — kicked off when the admin clicks Trigger."""

    run_id: UUID


# ── DB helpers ───────────────────────────────────────────────────────


def _build_engine():
    settings = get_settings()
    return create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )


def _slugify(value: str) -> str:
    """Conservative ASCII slug. Letters, digits, hyphens; max 60 chars."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]", "", value).strip().lower()
    cleaned = re.sub(r"[\s_-]+", "-", cleaned).strip("-")
    return (cleaned[:60].rstrip("-")) or "agent-post"


async def _unique_slug(session: AsyncSession, base: str) -> str:
    candidate = base
    suffix = 1
    while True:
        result = await session.execute(
            text("SELECT 1 FROM pages WHERE slug = :slug LIMIT 1").bindparams(
                slug=candidate
            )
        )
        if result.first() is None:
            return candidate
        suffix += 1
        candidate = f"{base}-{suffix}"[:60].rstrip("-")


# ── Stage runners ────────────────────────────────────────────────────


async def _run_stage(
    agent,
    user_prompt: str,
    *,
    run_id: UUID,
    root_session_id: str | None,
    session_maker,
    stage_name: str,
) -> tuple[str, Any]:
    """Run one Skrift Agent. Returns (session_id, typed result).

    Pre-allocates the session id and writes it to the run row BEFORE the
    agent.run() call so the SSE poller can subscribe to the event stream
    while the stage is in flight, rather than only seeing events after
    the stage completes.
    """
    sid = uuid4().hex
    # Record session id up-front for live SSE tailing.
    async with session_maker() as db:
        run = await db.get(AuthoringPipelineRun, run_id)
        if run is not None:
            ids = dict(run.stage_session_ids or {})
            ids[stage_name] = sid
            run.stage_session_ids = ids
            if root_session_id is None and run.root_session_id is None:
                run.root_session_id = UUID(sid)
            await db.commit()

    kwargs: dict[str, Any] = {
        "deps_ref": {"run_id": str(run_id)},
        "session_id": sid,
    }
    if root_session_id is not None:
        kwargs["parent_session_id"] = root_session_id
    session = await agent.run(user_prompt, **kwargs)
    result = await session.result()
    return sid, result


def _typed(result: Any, model_cls):
    if isinstance(result, model_cls):
        return result
    inner = getattr(result, "output", None)
    if isinstance(inner, model_cls):
        return inner
    raise RuntimeError(
        f"Expected {model_cls.__name__} from agent, got {type(result).__name__}"
    )


# ── Orchestrator handler ─────────────────────────────────────────────


@handler(
    "blogging.pipeline.run",
    queue="agents",
    max_attempts=1,
    # The whole 5-stage pipeline can run for several minutes; the default
    # 30s visibility timeout would let another worker re-claim the job
    # while it's still mid-Research and we'd end up running synthesis
    # twice. 1800s (30 min) is a comfortable ceiling.
    visibility_timeout=1800.0,
)
async def run_authoring_pipeline(payload: PipelineRunPayload) -> dict:
    """Run all five blogging-pipeline stages for one run row."""
    run_id = payload.run_id
    engine = _build_engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    cache = register_cache(str(run_id))

    try:
        async with Session() as db:
            run = await db.get(AuthoringPipelineRun, run_id)
            if run is None:
                logger.error("pipeline run %s missing — aborting", run_id)
                return {"status": "missing"}
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            await db.commit()

        # ── Stage 1: Review ──────────────────────────────────────────
        # Review trims + dedupes the candidate queue. Only worth running
        # when something *new* has shown up since the last run; with only
        # previously-`kept` rows in the inbox there's nothing fresh to
        # judge, so we skip the API call and leave the existing kept set
        # alone. Brainstorm + Scout still proceed regardless.
        async with Session() as db:
            result = await db.execute(
                select(CandidateBlogTopic)
                .where(CandidateBlogTopic.status.in_(("new", "kept")))
                .order_by(CandidateBlogTopic.surfaced_at.desc())
            )
            inbox = list(result.scalars())

        has_new = any(t.status == "new" for t in inbox)
        root_session_id: str | None = None
        if has_new:
            review_input = ReviewInput(
                candidates=[
                    CandidateTopicView(
                        id=t.id,
                        headline=t.headline,
                        observation=t.observation,
                        scope=t.scope,
                        evidence=list(t.evidence or []),
                        category=t.category,
                        status=t.status,
                        surfaced_at_iso=t.surfaced_at.isoformat(),
                        surfaced_by=t.surfaced_by,
                    )
                    for t in inbox
                ]
            )
            review_sid, review_raw = await _run_stage(
                review_agent,
                build_review_user_turn(review_input),
                run_id=run_id,
                root_session_id=None,
                session_maker=Session,
                stage_name="review",
            )
            review_out = _typed(review_raw, ReviewOutput)
            root_session_id = review_sid

            # Apply Review's decisions to the DB.
            kept_set = {tid for tid in review_out.kept_topic_ids}
            async with Session() as db:
                for topic in inbox:
                    new_status = "kept" if topic.id in kept_set else "discarded"
                    if new_status != topic.status:
                        await db.execute(
                            update(CandidateBlogTopic)
                            .where(CandidateBlogTopic.id == topic.id)
                            .values(
                                status=new_status,
                                reviewed_at=datetime.now(timezone.utc),
                            )
                        )
                await db.commit()
        else:
            logger.info(
                "pipeline run %s: no new candidates — skipping Review", run_id
            )

        # ── Stage 2: Scout ───────────────────────────────────────────
        scout_sid, scout_raw = await _run_stage(
            scout_agent,
            "Find 2-3 current tech-news topics worth a blog post.",
            run_id=run_id,
            root_session_id=root_session_id,
            session_maker=Session,
            stage_name="scout",
        )
        scout_out = _typed(scout_raw, ScoutOutput)

        # ── Stage 3: Brainstorm ──────────────────────────────────────
        async with Session() as db:
            result = await db.execute(
                select(CandidateBlogTopic)
                .where(CandidateBlogTopic.status == "kept")
                .order_by(CandidateBlogTopic.surfaced_at.desc())
                .limit(17)
            )
            kept_topics = list(result.scalars())

        brainstorm_candidates: list[BrainstormCandidate] = [
            BrainstormCandidate(
                source="kept",
                headline=t.headline,
                observation=t.observation,
                scope=t.scope or "",
                evidence=list(t.evidence or []),
                category=t.category,
            )
            for t in kept_topics
        ]
        for scout_topic in scout_out.topics:
            brainstorm_candidates.append(
                BrainstormCandidate(
                    source="scout",
                    headline=scout_topic.headline,
                    observation=scout_topic.observation,
                    scope=scout_topic.scope,
                    evidence=scout_topic.evidence,
                    category=scout_topic.category,
                )
            )

        # Empty candidate list is allowed — Brainstorm gets to decide
        # whether the queue + scout's news yielded anything worth a
        # hypothesis (it can return an abort via the schema's sentinel).
        brainstorm_input = BrainstormInput(candidates=brainstorm_candidates)
        brainstorm_sid, brainstorm_raw = await _run_stage(
            brainstorm_agent,
            build_brainstorm_user_turn(brainstorm_input),
            run_id=run_id,
            root_session_id=root_session_id,
            session_maker=Session,
            stage_name="brainstorm",
        )
        brainstorm_out = _typed(brainstorm_raw, BrainstormOutput)

        if brainstorm_out.is_abort:
            await _finalise_failed(
                Session,
                run_id,
                f"brainstorm aborted: {brainstorm_out.hypothesis}",
            )
            return {"status": "failed", "reason": "brainstorm_abort"}

        # ── Stage 4: Research ────────────────────────────────────────
        research_input = ResearchInput(
            hypothesis=brainstorm_out.hypothesis,
            counter_hypothesis=brainstorm_out.counter_hypothesis,
            open_questions=brainstorm_out.open_questions,
        )
        research_sid, research_raw = await _run_stage(
            research_agent,
            build_research_user_turn(research_input),
            run_id=run_id,
            root_session_id=root_session_id,
            session_maker=Session,
            stage_name="research",
        )
        research_out = _typed(research_raw, ResearchOutput)

        # ── Stage 5: Synthesis ───────────────────────────────────────
        synthesis_input = SynthesisInput(
            revised_hypothesis=research_out.revised_hypothesis,
            hypothesis_status=research_out.hypothesis_status,
            citations=research_out.citations,
            surprises=research_out.surprises,
            limits=research_out.limits,
        )
        synthesis_sid, synthesis_raw = await _run_stage(
            synthesis_agent,
            build_synthesis_user_turn(synthesis_input),
            run_id=run_id,
            root_session_id=root_session_id,
            session_maker=Session,
            stage_name="synthesis",
        )
        synthesis_out = _typed(synthesis_raw, SynthesisOutput)

        # ── Write the page row ───────────────────────────────────────
        page_id = await _write_blog_post(Session, synthesis_out)
        await _finalise_completed(Session, run_id, page_id, root_session_id)
        return {"status": "completed", "page_id": str(page_id)}

    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline run %s crashed", run_id)
        await _finalise_failed(Session, run_id, f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "reason": "exception"}
    finally:
        await drop_cache(str(run_id))
        await engine.dispose()


# ── Persistence helpers ──────────────────────────────────────────────


async def _finalise_completed(
    Session, run_id: UUID, page_id: UUID, root_session_id: str
) -> None:
    async with Session() as db:
        run = await db.get(AuthoringPipelineRun, run_id)
        if run is None:
            return
        run.status = "completed"
        run.result_page_id = page_id
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def _finalise_failed(Session, run_id: UUID, reason: str) -> None:
    async with Session() as db:
        run = await db.get(AuthoringPipelineRun, run_id)
        if run is None:
            return
        run.status = "failed"
        run.error = reason
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def _write_blog_post(Session, out: SynthesisOutput) -> UUID:
    """Insert a `pages` row + `blog_post_meta` row for the agent-authored post."""
    async with Session() as db:
        # Look up the Smarter Dev agent user.
        result = await db.execute(
            text("SELECT id FROM users WHERE email = :email").bindparams(
                email=SMARTER_DEV_AGENT_EMAIL
            )
        )
        row = result.first()
        if row is None:
            raise RuntimeError(
                f"Smarter Dev agent user ({SMARTER_DEV_AGENT_EMAIL}) is "
                "missing; migration may not have run."
            )
        author_id: UUID = row[0]

        slug = await _unique_slug(db, _slugify(out.slug or out.title))
        page_id = uuid4()
        now = datetime.now(timezone.utc)
        body = out.content.rstrip()
        if out.limits_paragraph.strip():
            body = (
                f"{body}\n\n"
                "## What this post doesn't cover\n\n"
                f"{out.limits_paragraph.strip()}\n"
            )
        await db.execute(
            text(
                """
                INSERT INTO pages
                    (id, slug, title, type, content, user_id, is_published,
                     published_at, meta_robots, "order", created_at, updated_at)
                VALUES
                    (:id, :slug, :title, 'blog', :content, :uid, true,
                     :published_at, 'noindex, nofollow', 0,
                     :created_at, :updated_at)
                """
            ).bindparams(
                id=page_id,
                slug=slug,
                title=out.title,
                content=body,
                uid=author_id,
                published_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(BlogPostMeta(page_id=page_id))
        await db.commit()
        return page_id
