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

import logging
import time
from collections import defaultdict
from typing import Optional
from uuid import UUID

from litestar import Controller, Request, post
from litestar.exceptions import HTTPException
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

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.lib.markdown import render_markdown

from smarter_dev.web.models import (
    AgentConversation,
    AgentMessage,
    ResourceSource,
)
from smarter_dev.web.resources_agent import begin_run, resource_agent
from smarter_dev.web.sdanswer import enrich_answer

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
    """Cheap derived title — first sentence or truncated question."""
    clean = " ".join(question.split())
    for sep in (". ", "? ", "! ", "\n"):
        if sep in clean:
            head = clean.split(sep, 1)[0] + sep.strip()
            if len(head) <= max_len:
                return head
            break
    return (clean[: max_len - 1] + "…") if len(clean) > max_len else clean


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


async def _run_agent_turn(
    db_session: AsyncSession,
    conversation: AgentConversation,
    question: str,
    message_history,
) -> AgentMessage:
    """Run the agent, persist the user turn + assistant turn, return the assistant turn."""
    # Fetch the next sequence number atomically-ish — we hold the row already
    # via the surrounding controller's commit.
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
    await db_session.flush()

    hits = begin_run()
    try:
        session = await resource_agent.run(question, message_history=message_history)
        # Skrift's Agent.run returns a Session — poll until completion to get
        # the final text the model produced.
        answer_text = await session.result()
    except Exception as exc:  # noqa: BLE001
        # Surface a clean error rather than 500-stacktracing into the JSON.
        msg = str(exc)
        logger.exception("Resource agent run failed")
        if "GEMINI_API_KEY" in msg or "GOOGLE_API_KEY" in msg or "api_key" in msg.lower():
            raise HTTPException(
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent not configured. Try again later.",
            )
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent failed to respond. Try again in a moment.",
        )

    if not isinstance(answer_text, str):
        answer_text = getattr(answer_text, "output", None) or str(answer_text)
    citations = await _resolve_citations(db_session, hits)
    usage = None

    assistant_turn = AgentMessage(
        conversation_id=conversation.id,
        sequence=last_seq + 2,
        role="assistant",
        content=answer_text,
        citations=citations,
        usage=usage,
    )
    db_session.add(assistant_turn)
    return assistant_turn


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
        user_id = _require_user_id(request)
        _enforce_rate(_ask_log, str(user_id), _ASK_LIMIT)

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

        assistant_turn = await _run_agent_turn(
            db_session, conversation, question, message_history=None
        )
        await db_session.commit()

        return {
            "id": str(conversation.id),
            "url": f"/ai/answer/{conversation.id}",
            "answer": assistant_turn.content,
            "citations": list(assistant_turn.citations or []),
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

        # Load prior messages to feed back to the agent as message_history.
        msg_result = await db_session.execute(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == conversation.id)
            .order_by(AgentMessage.sequence.asc())
        )
        prior = msg_result.scalars().all()
        message_history = _build_message_history(prior)

        assistant_turn = await _run_agent_turn(
            db_session, conversation, question, message_history=message_history
        )
        await db_session.commit()

        # Reload the user turn we just appended so we can return both turns.
        await db_session.refresh(assistant_turn)
        user_turn_q = await db_session.execute(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.sequence == assistant_turn.sequence - 1,
            )
            .limit(1)
        )
        user_turn = user_turn_q.scalar_one()

        return {
            "user_message": await _message_to_dict(db_session, user_turn),
            "assistant_message": await _message_to_dict(db_session, assistant_turn),
        }


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
