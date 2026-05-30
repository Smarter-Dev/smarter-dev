"""HTTP API for the persisted agent conversations.

* ``POST /v2/api/resources/ask`` — start a new resource-agent conversation.
* ``POST /v2/api/agent/conversations/{conversation_id}/reply`` — owner-only
  follow-up turn within an existing conversation.

Both endpoints require a logged-in user (Skrift session) and rate-limit per
user. Both write to ``agent_conversations`` / ``agent_messages``. Citations
are drained from the agent's ``ContextVar`` after each run and resolved
against ``resource_sources`` so the rendered cards link to real curated
entries (with the existing ``track_key`` for click counting).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Optional
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.response import Response
from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
)
from msgspec import Struct
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.services import get_user_permissions
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.lib.markdown import render_markdown
from skrift.lib.notifications import notify_user

from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.models import (
    AgentConversation,
    AgentMessage,
    ResourceSource,
)
from smarter_dev.web.resources_agent import begin_run, run_resources_pipeline
from smarter_dev.web.sdanswer import enrich_answer
from smarter_dev.web.title_agent import generate_title

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiting (per user, not per IP, since asking requires login)
# ---------------------------------------------------------------------------

_ASK_LIMIT = 5
_REPLY_LIMIT = 10
_WINDOW = 60.0  # seconds
_ask_log: dict[str, list[float]] = defaultdict(list)
_reply_log: dict[str, list[float]] = defaultdict(list)


def _enforce_rate(bucket: dict[str, list[float]], key: str, limit: int) -> None:
    now = time.monotonic()
    timestamps = [t for t in bucket[key] if now - t < _WINDOW]
    if len(timestamps) >= limit:
        bucket[key] = timestamps
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Slow down a bit, will you?",
        )
    timestamps.append(now)
    bucket[key] = timestamps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Weekly quotas (the per-minute `_enforce_rate` above is for anti-spam burst
# control; these gate sustained usage per role tier).
#
# Tiers (highest wins):
#   sudo-rwx / administrator -> 50 questions/week, 10 follow-ups per answer
#   sudo-rw                  -> 30 / 10
#   sudo-r                   -> 20 / 10
#   everyone else            ->  0 /  0  (asking is a founder benefit)
# ---------------------------------------------------------------------------


def resources_weekly_quota(perms) -> tuple[int, int]:
    """Return ``(max_questions_per_week, max_followups_per_answer)``."""
    if (
        "administrator" in perms.permissions
        or "sudo-rwx" in perms.roles
    ):
        return 50, 10
    if "sudo-rw" in perms.roles:
        return 30, 10
    if "sudo-r" in perms.roles:
        return 20, 10
    return 0, 0


# Back-compat alias (the leading underscore version was the original name).
_resources_weekly_quota = resources_weekly_quota


async def resources_quota_state(
    db_session: AsyncSession,
    user_id: UUID,
    *,
    conversation_id: UUID | None = None,
) -> dict:
    """Compute the user's current resources-agent quota state.

    Returns a dict with:
      - ``max_questions``: weekly cap for the user's tier
      - ``used_questions``: count of resources conversations in the last 7d
      - ``questions_remaining``: max - used (>= 0)
      - ``max_followups``: per-answer follow-up cap for the user's tier
      - ``used_followups``: follow-ups already made on the given conversation
        (0 when ``conversation_id`` is None)
      - ``followups_remaining``: max - used (>= 0)
    """
    perms = await get_user_permissions(db_session, user_id)
    max_q, max_f = resources_weekly_quota(perms)
    used_q = await _count_questions_last_week(db_session, user_id, "resources")

    if conversation_id is not None:
        user_turns = await _count_user_turns(db_session, conversation_id)
        used_f = max(0, user_turns - 1)
    else:
        used_f = 0

    return {
        "max_questions": max_q,
        "used_questions": used_q,
        "questions_remaining": max(0, max_q - used_q),
        "max_followups": max_f,
        "used_followups": used_f,
        "followups_remaining": max(0, max_f - used_f),
    }


async def _count_questions_last_week(
    db_session: AsyncSession, user_id: UUID, agent_type: str
) -> int:
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    stmt = (
        select(func.count(AgentConversation.id))
        .where(AgentConversation.owner_user_id == user_id)
        .where(AgentConversation.agent_type == agent_type)
        .where(AgentConversation.created_at >= cutoff)
    )
    return int((await db_session.execute(stmt)).scalar() or 0)


async def _count_user_turns(
    db_session: AsyncSession, conversation_id: UUID
) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.count(AgentMessage.id))
        .where(AgentMessage.conversation_id == conversation_id)
        .where(AgentMessage.role == "user")
    )
    return int((await db_session.execute(stmt)).scalar() or 0)


async def _enforce_weekly_question_quota(
    db_session: AsyncSession, request: Request, user_id: UUID
) -> None:
    """Raise 429 if the user has used their weekly question allowance."""
    perms = await get_user_permissions(db_session, user_id)
    max_questions, _ = _resources_weekly_quota(perms)
    used = await _count_questions_last_week(db_session, user_id, "resources")
    if used >= max_questions:
        if max_questions <= 0:
            detail = "Asking is not enabled for your account."
        else:
            detail = (
                f"Weekly limit reached ({used}/{max_questions} questions "
                "in the last 7 days). The window rolls forward as your "
                "earliest question ages out."
            )
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS, detail=detail
        )


async def _enforce_followup_quota(
    db_session: AsyncSession,
    request: Request,
    user_id: UUID,
    conversation_id: UUID,
) -> None:
    """Raise 429 if the conversation has used its follow-up allowance."""
    perms = await get_user_permissions(db_session, user_id)
    _, max_followups = _resources_weekly_quota(perms)
    user_turns = await _count_user_turns(db_session, conversation_id)
    # `user_turns` includes the original question; the follow-up budget is
    # the additional user turns the asker is allowed to add.
    if user_turns >= 1 + max_followups:
        if max_followups <= 0:
            detail = "Follow-ups aren't enabled on your account."
        else:
            detail = (
                f"Follow-up limit reached ({user_turns - 1}/{max_followups} "
                "replies in this answer). Start a fresh question on "
                "/resources to keep going."
            )
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS, detail=detail
        )


def _require_user_id(request: Request) -> UUID:
    """Return the current session's user id or raise 401."""
    raw = request.session.get(SESSION_USER_ID) if request.session else None
    if not raw:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Sign in to ask the Resource Agent.",
        )
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        )


def _derive_title(question: str, max_len: int = 80) -> str:
    """Cheap derived title — first sentence or truncated question.

    Used as the placeholder while ``generate_title`` (Gemini Flash Lite) is
    still in flight on the background task.
    """
    clean = " ".join(question.split())
    for sep in (". ", "? ", "! ", "\n"):
        if sep in clean:
            head = clean.split(sep, 1)[0] + sep.strip()
            if len(head) <= max_len:
                return head
            break
    return (clean[: max_len - 1] + "…") if len(clean) > max_len else clean


# Hold strong refs to in-flight title tasks so the GC doesn't kill them
# mid-run while their fire-and-forget creator has long since returned.
_TITLE_TASKS: set[asyncio.Task] = set()
_RUN_TASKS: set[asyncio.Task] = set()


def _kick_title_generation(
    conversation_id: UUID, owner_user_id: UUID, question: str
) -> None:
    """Fire-and-forget: generate a real title, persist it, notify the owner.

    Runs in its own DB session because the request-scoped one closes the
    moment we return the JSON response.
    """

    async def _run() -> None:
        try:
            title = await generate_title(question, actor=str(owner_user_id))
            if not title:
                return
            # NB: must be the *Skrift* session context — agent_conversations
            # lives in the main DB under the `skrift` schema. The plain
            # `get_db_session_context()` targets the legacy bot-admin DB.
            async with get_skrift_db_session_context() as bg_session:
                conv = await bg_session.get(AgentConversation, conversation_id)
                if conv is None:
                    return
                conv.title = title
                await bg_session.commit()
            await notify_user(
                str(owner_user_id),
                "agent_title_updated",
                conversation_id=str(conversation_id),
                title=title,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Background title generation failed for conversation %s",
                conversation_id,
            )

    task = asyncio.create_task(_run())
    _TITLE_TASKS.add(task)
    task.add_done_callback(_TITLE_TASKS.discard)


async def _resolve_citations(
    db_session: AsyncSession, hits: list[dict]
) -> list[dict]:
    """Build the citation list to persist alongside an assistant turn.

    Priority order: every URL the agent fetched via ``read_source`` (in order),
    deduped. Falls back to the top three ``search_resources`` hits if the agent
    answered without reading anything.

    Each citation is resolved against ``resource_sources`` so the rendered card
    matches the curated entry exactly (title, byline, blurb, ``track_key`` for
    click tracking).
    """
    if not hits:
        return []

    read_urls: list[str] = []
    seen_read: set[str] = set()
    for hit in hits:
        if hit.get("source") == "read":
            url = hit.get("url")
            if url and url not in seen_read:
                seen_read.add(url)
                read_urls.append(url)

    fallback_urls: list[str] = []
    if not read_urls:
        seen_search: set[str] = set()
        for hit in hits:
            if hit.get("source") == "search" and hit.get("kind") == "source":
                url = hit.get("url")
                if url and url not in seen_search:
                    seen_search.add(url)
                    fallback_urls.append(url)
                if len(fallback_urls) >= 3:
                    break

    candidate_urls = read_urls or fallback_urls
    if not candidate_urls:
        return []

    result = await db_session.execute(
        select(ResourceSource).where(ResourceSource.url.in_(candidate_urls))
    )
    sources_by_url = {src.url: src for src in result.scalars().all()}

    # Map directory (via spine placement or tool placement) for each source.
    # Cheap path: read it from the original hits if present; the curated
    # ResourceSource doesn't carry the directory directly.
    directory_by_url: dict[str, str] = {}
    category_by_url: dict[str, str] = {}
    for hit in hits:
        url = hit.get("url")
        if url and hit.get("directory") and url not in directory_by_url:
            directory_by_url[url] = hit.get("directory") or ""
            category_by_url[url] = hit.get("category") or ""

    citations: list[dict] = []
    for url in candidate_urls:
        src = sources_by_url.get(url)
        if src is None:
            continue
        citations.append(
            {
                "title": src.title,
                "url": src.url,
                "byline": src.byline or "",
                "blurb": src.blurb or "",
                "learning_type": src.learning_type,
                "track_key": src.track_key,
                "directory": directory_by_url.get(url, ""),
                "category": category_by_url.get(url, ""),
            }
        )
    return citations


def _coerce_usage(result_usage) -> Optional[dict]:
    """Pull token counts off a pydantic-ai RunResult.usage object if present."""
    if result_usage is None:
        return None
    try:
        # pydantic_ai's Usage exposes input_tokens / output_tokens / total_tokens
        usage = result_usage() if callable(result_usage) else result_usage
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    except Exception:  # noqa: BLE001
        return None


def _kick_agent_run(
    conversation_id: UUID,
    owner_user_id: UUID,
    question: str,
) -> None:
    """Fire-and-forget: run the resource agent and finalize the answer.

    Skrift's agent runs are dispatched to the workers queue (the in-process
    ``local`` preset still runs them on the event loop, but the API contract
    is the same as a remote worker — we get a ``Session`` handle back
    immediately). This helper:

    1. Opens its own DB session (the request-scoped one is closed by now).
    2. Queues the run with ``deps_ref`` carrying the conversation + owner
       ids so tool implementations can emit ``agent_tool_event``
       notifications.
    3. Waits on ``session.result()``, persists the assistant turn with
       resolved citations, and fires ``agent_run_complete`` so the open
       browser tab swaps the tool stream for the final markdown.
    4. On exception, fires ``agent_run_error`` with the detail.
    """

    async def _run() -> None:
        try:
            hits: list[dict] = []
            stub = os.getenv("RESOURCE_AGENT_STUB", "").strip().lower() in {
                "1", "true", "yes",
            }
            if stub:
                # Local dev/debug short-circuit: skip the real Gemini call
                # and any tool invocations, sleep briefly to mimic latency,
                # and return a fixed answer. Keeps the title agent + the
                # rest of the streaming flow honest.
                await asyncio.sleep(2.0)
                answer_text = (
                    "[stub] resource_agent is disabled via "
                    "`RESOURCE_AGENT_STUB=1`; no Gemini call was made.\n\n"
                    "This is a placeholder answer so we can debug the live "
                    "title typewriter and run-complete reveal without "
                    "burning tokens."
                )
            else:
                # Load prior turns so follow-ups inherit the conversation's
                # context. For an initial ask there's only the just-committed
                # user turn — drop it (the agent receives it as `question`).
                async with get_skrift_db_session_context() as history_session:
                    prior_q = await history_session.execute(
                        select(AgentMessage)
                        .where(AgentMessage.conversation_id == conversation_id)
                        .order_by(AgentMessage.sequence.asc())
                    )
                    prior = list(prior_q.scalars().all())
                # Drop the latest user turn — it's `question` itself; the
                # agent gets it via the prompt arg.
                if prior and prior[-1].role == "user":
                    prior = prior[:-1]
                message_history = (
                    _build_message_history(prior) if prior else None
                )

                hits = begin_run()
                answer_text = await run_resources_pipeline(
                    question,
                    message_history=message_history,
                    actor=str(owner_user_id),
                    conversation_id=str(conversation_id),
                    owner_user_id=str(owner_user_id),
                )

            async with get_skrift_db_session_context() as bg_session:
                conversation = await bg_session.get(
                    AgentConversation, conversation_id
                )
                if conversation is None:
                    return

                next_seq_q = await bg_session.execute(
                    select(AgentMessage.sequence)
                    .where(AgentMessage.conversation_id == conversation_id)
                    .order_by(AgentMessage.sequence.desc())
                    .limit(1)
                )
                last_seq = next_seq_q.scalar_one_or_none() or 0

                assistant_turn = AgentMessage(
                    conversation_id=conversation_id,
                    sequence=last_seq + 1,
                    role="assistant",
                    content=answer_text,
                    citations=[],
                )
                bg_session.add(assistant_turn)
                await bg_session.commit()
                await bg_session.refresh(assistant_turn)

                content_html, blocks = await enrich_answer(
                    bg_session, answer_text
                )

            await notify_user(
                str(owner_user_id),
                "agent_run_complete",
                conversation_id=str(conversation_id),
                assistant_message_id=str(assistant_turn.id),
                content_html=content_html,
                sdanswer_blocks=blocks,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "agent run finalize failed for conversation %s", conversation_id
            )
            msg = str(exc)
            if (
                "GEMINI_API_KEY" in msg
                or "GOOGLE_API_KEY" in msg
                or "api_key" in msg.lower()
            ):
                detail = "Agent not configured. Try again later."
            else:
                detail = "Agent failed to respond. Try again in a moment."
            try:
                await notify_user(
                    str(owner_user_id),
                    "agent_run_error",
                    conversation_id=str(conversation_id),
                    detail=detail,
                )
            except Exception:  # noqa: BLE001
                logger.exception("agent_run_error notify_user failed")

    task = asyncio.create_task(_run())
    _RUN_TASKS.add(task)
    task.add_done_callback(_RUN_TASKS.discard)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class AskBody(Struct):
    """Request body for POST /v2/api/resources/ask."""

    question: str
    level: str = "senior"


class ReplyBody(Struct):
    """Request body for POST /v2/api/agent/conversations/{id}/reply."""

    question: str


def _validate_question(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Question is required.",
        )
    if len(cleaned) > 1000:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Question is too long (max 1000 characters).",
        )
    return cleaned


def _validate_level(level: str) -> str:
    level = (level or "senior").strip().lower()
    if level not in {"junior", "senior"}:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Level must be 'junior' or 'senior'.",
        )
    return level


async def _message_to_dict(
    db_session: AsyncSession, msg: AgentMessage
) -> dict:
    if msg.role == "assistant":
        content_html, blocks = await enrich_answer(db_session, msg.content or "")
    else:
        content_html = render_markdown(msg.content or "")
        blocks = []
    return {
        "id": str(msg.id),
        "sequence": msg.sequence,
        "role": msg.role,
        "content": msg.content,
        "content_html": content_html,
        "citations": list(msg.citations or []),
        "sdanswer_blocks": blocks,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------


class ResourcesAgentApiController(Controller):
    """``POST /v2/api/resources/ask`` — start a new resources conversation."""

    path = "/v2/api/resources"

    @post("/ask", status_code=HTTP_201_CREATED)
    async def ask(
        self, data: AskBody, request: Request, db_session: AsyncSession
    ) -> dict:
        """Create the conversation + user turn and return immediately.

        The agent run is queued in the background; the browser subscribes
        to ``agent_tool_event`` / ``agent_run_complete`` notifications on
        the conversation to animate tool calls and the final answer in
        place. This handler aims to complete in under 200ms so the
        client-side morph from /resources to /ai/answer/{id} doesn't
        stall on the form submit.
        """
        user_id = _require_user_id(request)
        _enforce_rate(_ask_log, str(user_id), _ASK_LIMIT)
        await _enforce_weekly_question_quota(db_session, request, user_id)

        question = _validate_question(data.question)
        level = _validate_level(data.level)

        conversation = AgentConversation(
            agent_type="resources",
            owner_user_id=user_id,
            title=_derive_title(question),
            meta={"level": level},
        )
        db_session.add(conversation)
        await db_session.flush()

        user_turn = AgentMessage(
            conversation_id=conversation.id,
            sequence=1,
            role="user",
            content=question,
            citations=[],
        )
        db_session.add(user_turn)
        await db_session.commit()
        await db_session.refresh(user_turn)

        # Title generation + agent run both happen in the background. The
        # browser stays on /resources (morphed into the answer layout) and
        # picks up notifications as each completes.
        _kick_title_generation(conversation.id, user_id, question)
        _kick_agent_run(conversation.id, user_id, question)

        return {
            "id": str(conversation.id),
            "url": f"/ai/answer/{conversation.id}",
            "user_message": {
                "id": str(user_turn.id),
                "sequence": user_turn.sequence,
                "content": user_turn.content,
                "created_at": (
                    user_turn.created_at.isoformat()
                    if user_turn.created_at else None
                ),
            },
        }


class AgentConversationApiController(Controller):
    """``POST /v2/api/agent/conversations/{id}/reply`` — owner-only follow-up."""

    path = "/v2/api/agent/conversations"

    @post("/{conversation_id:uuid}/reply", status_code=HTTP_201_CREATED)
    async def reply(
        self,
        conversation_id: UUID,
        data: ReplyBody,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        """Commit the user's follow-up + return immediately.

        Mirrors `ResourcesAgentApiController.ask`: the agent run is
        queued in the background; the browser subscribes to the same
        `sk:notification` types as the initial ask (`agent_tool_event`,
        `agent_run_complete`) to animate the new turn in place.
        """
        user_id = _require_user_id(request)
        _enforce_rate(_reply_log, str(user_id), _REPLY_LIMIT)

        question = _validate_question(data.question)

        result = await db_session.execute(
            select(AgentConversation).where(AgentConversation.id == conversation_id)
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND, detail="Conversation not found."
            )
        if conversation.owner_user_id != user_id:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail="Only the original asker can reply here.",
            )
        await _enforce_followup_quota(
            db_session, request, user_id, conversation.id
        )

        next_seq_q = await db_session.execute(
            select(AgentMessage.sequence)
            .where(AgentMessage.conversation_id == conversation.id)
            .order_by(AgentMessage.sequence.desc())
            .limit(1)
        )
        last_seq = next_seq_q.scalar_one_or_none() or 0

        user_turn = AgentMessage(
            conversation_id=conversation.id,
            sequence=last_seq + 1,
            role="user",
            content=question,
            citations=[],
        )
        db_session.add(user_turn)
        await db_session.commit()
        await db_session.refresh(user_turn)

        _kick_agent_run(conversation.id, user_id, question)

        # Recompute the remaining follow-ups *after* the just-committed
        # user turn so the client can decrement its counter from the
        # returned number.
        quota = await resources_quota_state(
            db_session, user_id, conversation_id=conversation.id
        )

        return {
            "user_message": {
                "id": str(user_turn.id),
                "sequence": user_turn.sequence,
                "content": user_turn.content,
                "created_at": (
                    user_turn.created_at.isoformat()
                    if user_turn.created_at else None
                ),
            },
            "followups_remaining": quota["followups_remaining"],
            "max_followups": quota["max_followups"],
        }


class AgentMessageApiController(Controller):
    """Read-only access to a stored ``AgentMessage`` body.

    The COPY button on /ai/answer/{id} fetches `/markdown` at click time
    instead of carrying a duplicate of every assistant turn in the page
    source. Same access policy as the answer page itself: anyone with the
    link can read (the answer pages are private-but-not-protected; share
    URLs are intentionally readable to anyone who has them).
    """

    path = "/v2/api/agent/messages"

    @get("/{message_id:uuid}/markdown")
    async def markdown(
        self, message_id: UUID, db_session: AsyncSession
    ) -> Response:
        msg = await db_session.get(AgentMessage, message_id)
        if msg is None:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND, detail="Message not found."
            )
        return Response(
            content=msg.content or "",
            media_type="text/markdown; charset=utf-8",
            headers={"Cache-Control": "private, max-age=300"},
        )


def _build_message_history(prior: list[AgentMessage]):
    """Convert persisted turns into pydantic-ai ModelMessages for replay."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    history = []
    for msg in prior:
        if msg.role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
        elif msg.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=msg.content)]))
    return history
