"""Resources-agent run as a Skrift worker job.

The web ``/v2/api/resources/ask`` and ``.../reply`` controllers used to run the
whole resource-agent pipeline inline on the web event loop (loading history,
building pydantic-ai message objects, running the pipeline, persisting the
answer, notifying the browser). That pulled pydantic-ai into the web process.

Here that orchestration lives behind a worker ``@handler``: the web controllers
just ``worker_submit(ResourcesRunPayload(...))`` and return; this handler runs
entirely in the agent-worker tier, where pydantic-ai lives. The module is kept
import-clean of pydantic-ai (the message-history conversion imports it lazily)
so the web process can import ``ResourcesRunPayload`` to dispatch jobs without
loading the inference stack.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from pydantic import BaseModel
from skrift.notifications import notify_user
from skrift.workers import handler
from sqlalchemy import select

from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.models import AgentConversation, AgentMessage
from smarter_dev.web.resources_agent import begin_run, run_resources_pipeline
from smarter_dev.web.sdanswer import enrich_answer

logger = logging.getLogger(__name__)


class ResourcesRunPayload(BaseModel):
    """Job payload for a single resource-agent run on a conversation."""

    conversation_id: str
    owner_user_id: str
    question: str


def _build_message_history(prior: list[AgentMessage]):
    """Convert persisted turns into pydantic-ai ModelMessages for replay.

    Imported lazily so this module stays pydantic-ai-free at import time (it
    runs only in the worker that executes the agent)."""
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


@handler(
    "resources.agent.run",
    queue="agents",
    max_attempts=1,
    # The reframer→researcher→gap-filler→author pipeline plus its sub-agent /
    # tool-call dispatch can run for a couple of minutes; keep a generous
    # visibility timeout so the job isn't re-claimed mid-run.
    visibility_timeout=600.0,
)
async def run_resources_job(payload: ResourcesRunPayload) -> dict:
    """Run the resource agent for a conversation and finalize the answer.

    Loads prior turns, replays them as history, runs the pipeline, persists the
    assistant turn with enriched HTML, and fires ``agent_run_complete`` (or
    ``agent_run_error``) so the open browser tab swaps in the answer."""
    conversation_id = UUID(payload.conversation_id)
    owner_user_id = UUID(payload.owner_user_id)
    question = payload.question
    try:
        stub = os.getenv("RESOURCE_AGENT_STUB", "").strip().lower() in {
            "1", "true", "yes",
        }
        if stub:
            answer_text = (
                "[stub] resource_agent is disabled via "
                "`RESOURCE_AGENT_STUB=1`; no Gemini call was made."
            )
        else:
            async with get_skrift_db_session_context() as history_session:
                prior_q = await history_session.execute(
                    select(AgentMessage)
                    .where(AgentMessage.conversation_id == conversation_id)
                    .order_by(AgentMessage.sequence.asc())
                )
                prior = list(prior_q.scalars().all())
            # Drop the latest user turn — it's `question` itself; the agent
            # gets it via the prompt arg.
            if prior and prior[-1].role == "user":
                prior = prior[:-1]
            message_history = _build_message_history(prior) if prior else None

            begin_run()
            answer_text = await run_resources_pipeline(
                question,
                message_history=message_history,
                actor=str(owner_user_id),
                conversation_id=str(conversation_id),
                owner_user_id=str(owner_user_id),
            )

        async with get_skrift_db_session_context() as bg_session:
            conversation = await bg_session.get(AgentConversation, conversation_id)
            if conversation is None:
                return {"status": "missing"}

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

            content_html, blocks = await enrich_answer(bg_session, answer_text)

        await notify_user(
            str(owner_user_id),
            "agent_run_complete",
            conversation_id=str(conversation_id),
            assistant_message_id=str(assistant_turn.id),
            content_html=content_html,
            sdanswer_blocks=blocks,
        )
        return {"status": "ok", "assistant_message_id": str(assistant_turn.id)}
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "resources agent run failed for conversation %s", conversation_id
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
        return {"status": "error"}
