"""Durable, idempotent Resources-agent worker orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from skrift.db.models.user import User
from skrift.notifications import NotificationMode
from skrift.notifications import notify_user
from skrift.workers import handler
from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.web.chat.usage import record_usage
from smarter_dev.web.models import AgentMessage
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.resources_agent import begin_run
from smarter_dev.web.resources_agent import run_resources_pipeline
from smarter_dev.web.sdanswer import enrich_answer

logger = logging.getLogger(__name__)


class ResourcesRunPayload(BaseModel):
    # Legacy fields remain during rolling deploys: old workers ignore run_id,
    # while new workers materialize a durable run for old queued payloads.
    run_id: str | None = None
    conversation_id: str | None = None
    owner_user_id: str | None = None
    question: str | None = None


def _build_message_history(prior: list[AgentMessage]):
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.messages import TextPart
    from pydantic_ai.messages import UserPromptPart

    history = []
    for message in prior:
        if message.role == "user":
            history.append(
                ModelRequest(parts=[UserPromptPart(content=message.content)])
            )
        elif message.role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=message.content)]))
    return history


def _catalog_model(model_name: str | None):
    if not model_name:
        return None
    wire = model_name.split(":", 1)[-1]
    return next(
        (
            model
            for model in MODEL_CATALOG
            if wire == model.model_id or wire.startswith(model.model_id)
        ),
        None,
    )


async def _persist_stage_usage(
    session, run: ResourceAgentRun, stages: list[dict]
) -> None:
    for index, stage in enumerate(stages):
        operation_index = int(stage.get("_operation_index", index))
        model = _catalog_model(stage.get("model_name"))
        if model is None:
            logger.warning("Skipping unrecognized Resources stage model %r", stage)
            continue
        await record_usage(
            session,
            operation_key=(
                f"resources:{run.id}:attempt:{run.attempt_count}:"
                f"{stage.get('stage', 'unknown')}:{operation_index}"
            ),
            product_mode="resources",
            operation_type=f"resource_{stage.get('stage', 'unknown')}",
            model=model,
            input_tokens=int(stage.get("input_tokens") or 0),
            output_tokens=int(stage.get("output_tokens") or 0),
            cache_read_tokens=int(stage.get("cache_read_tokens") or 0),
            cache_write_tokens=int(stage.get("cache_write_tokens") or 0),
            user_id=run.owner_user_id,
            conversation_id=run.conversation_id,
            root_turn_id=run.id,
            details={"resource_run_id": str(run.id)},
        )


async def _heartbeat(run_id: UUID, token: str) -> None:
    while True:
        await asyncio.sleep(10)
        async with get_db_session_context() as session:
            run = await session.scalar(
                select(ResourceAgentRun).where(
                    ResourceAgentRun.id == run_id,
                    ResourceAgentRun.worker_lease_token == token,
                    ResourceAgentRun.status == "running",
                )
            )
            if run is None:
                return
            run.worker_lease_expires_at = datetime.now(UTC) + timedelta(seconds=660)
            await session.commit()


async def _notify_safe(user_id: UUID, event: str, **payload) -> None:
    try:
        await notify_user(
            str(user_id), event, mode=NotificationMode.EPHEMERAL, **payload
        )
    except Exception:
        logger.exception("Resources notification failed after durable state commit")


@handler(
    "resources.agent.run",
    queue="agents",
    max_attempts=5,
    visibility_timeout=600.0,
)
async def run_resources_job(payload: ResourcesRunPayload) -> dict:
    if payload.run_id:
        run_id = UUID(payload.run_id)
    else:
        if not (payload.conversation_id and payload.owner_user_id and payload.question):
            raise ValueError("invalid Resources worker payload")
        conversation_id = UUID(payload.conversation_id)
        owner_user_id = UUID(payload.owner_user_id)
        async with get_db_session_context() as session:
            user_message = await session.scalar(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.role == "user",
                    AgentMessage.content == payload.question,
                )
                .order_by(AgentMessage.sequence.desc())
                .limit(1)
            )
            if user_message is None:
                return {"status": "missing"}
            run = await session.scalar(
                select(ResourceAgentRun).where(
                    ResourceAgentRun.conversation_id == conversation_id,
                    ResourceAgentRun.user_sequence == user_message.sequence,
                )
            )
            if run is None:
                run = ResourceAgentRun(
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                    user_sequence=user_message.sequence,
                    submission_key=(
                        f"legacy:{conversation_id}:{user_message.sequence}"
                    )[:128],
                    question=payload.question,
                    status="submitted",
                )
                session.add(run)
                await session.commit()
            run_id = run.id
    token = uuid4().hex
    now = datetime.now(UTC)
    async with get_db_session_context() as session:
        run = await session.scalar(
            select(ResourceAgentRun)
            .where(ResourceAgentRun.id == run_id)
            .with_for_update()
        )
        if run is None:
            return {"status": "missing"}
        if run.status in {"complete", "error", "cancelled"}:
            return {"status": run.status, "idempotent": True}
        owner = await session.get(User, run.owner_user_id)
        if owner is None or not owner.is_active:
            run.status = "cancelled"
            run.error = "account is inactive"
            run.finished_at = now
            await session.commit()
            return {"status": "cancelled"}
        if (
            run.status == "running"
            and run.worker_lease_expires_at is not None
            and run.worker_lease_expires_at > now
        ):
            return {"status": "running", "idempotent": True}
        run.status = "running"
        run.worker_lease_token = token
        run.worker_lease_expires_at = now + timedelta(seconds=660)
        run.attempt_count += 1
        conversation_id = run.conversation_id
        owner_user_id = run.owner_user_id
        question = run.question
        user_sequence = run.user_sequence
        existing_assistant = await session.scalar(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.sequence == user_sequence + 1,
                AgentMessage.role == "assistant",
            )
        )
        if existing_assistant is not None:
            # A legacy worker from a rolling deployment may already have
            # completed this payload without knowing ResourceAgentRun.
            run.status = "complete"
            run.finished_at = datetime.now(UTC)
            run.worker_lease_token = None
            run.worker_lease_expires_at = None
            await session.commit()
            return {
                "status": "complete",
                "assistant_message_id": str(existing_assistant.id),
                "legacy_worker": True,
            }
        await session.commit()

    heartbeat = asyncio.create_task(_heartbeat(run_id, token))
    stage_usage: list[dict] = []
    try:
        stub = os.getenv("RESOURCE_AGENT_STUB", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if stub:
            answer_text = (
                "[stub] resource_agent is disabled via `RESOURCE_AGENT_STUB=1`; "
                "no provider call was made."
            )
        else:
            async with get_db_session_context() as history_session:
                prior = list(
                    (
                        await history_session.execute(
                            select(AgentMessage)
                            .where(
                                AgentMessage.conversation_id == conversation_id,
                                AgentMessage.sequence < user_sequence,
                            )
                            .order_by(AgentMessage.sequence)
                        )
                    ).scalars()
                )
            begin_run()

            async def persist_stage(record: dict, index: int) -> None:
                durable_record = {**record, "_operation_index": index}
                async with get_db_session_context() as usage_session:
                    durable_run = await usage_session.scalar(
                        select(ResourceAgentRun).where(
                            ResourceAgentRun.id == run_id,
                            ResourceAgentRun.worker_lease_token == token,
                            ResourceAgentRun.status == "running",
                        )
                    )
                    if durable_run is None:
                        raise asyncio.CancelledError
                    await _persist_stage_usage(
                        usage_session, durable_run, [durable_record]
                    )
                    await usage_session.commit()

            answer_text = await run_resources_pipeline(
                question,
                message_history=_build_message_history(prior) if prior else None,
                actor=str(owner_user_id),
                conversation_id=str(conversation_id),
                owner_user_id=str(owner_user_id),
                usage_records=stage_usage,
                usage_callback=persist_stage,
            )

        async with get_db_session_context() as session:
            run = await session.scalar(
                select(ResourceAgentRun)
                .where(
                    ResourceAgentRun.id == run_id,
                    ResourceAgentRun.worker_lease_token == token,
                    ResourceAgentRun.status == "running",
                )
                .with_for_update()
            )
            if run is None:
                return {"status": "superseded"}
            if run.status == "complete":
                return {"status": "complete", "idempotent": True}
            existing = await session.scalar(
                select(AgentMessage).where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.sequence == user_sequence + 1,
                    AgentMessage.role == "assistant",
                )
            )
            assistant = existing or AgentMessage(
                conversation_id=conversation_id,
                sequence=user_sequence + 1,
                role="assistant",
                content=answer_text,
                citations=[],
                usage={"stages": stage_usage},
            )
            if existing is None:
                session.add(assistant)
            await _persist_stage_usage(session, run, stage_usage)
            run.status = "complete"
            run.finished_at = datetime.now(UTC)
            run.worker_lease_token = None
            run.worker_lease_expires_at = None
            await session.commit()
            await session.refresh(assistant)
            content_html, blocks = await enrich_answer(session, answer_text)

        await _notify_safe(
            owner_user_id,
            "agent_run_complete",
            conversation_id=str(conversation_id),
            assistant_message_id=str(assistant.id),
            content_html=content_html,
            sdanswer_blocks=blocks,
        )
        return {"status": "ok", "assistant_message_id": str(assistant.id)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Resources run failed for %s", run_id)
        detail = (
            "Agent not configured. Try again later."
            if "api_key" in str(exc).lower()
            else "Agent failed to respond. Try again in a moment."
        )
        async with get_db_session_context() as session:
            run = await session.scalar(
                select(ResourceAgentRun)
                .where(
                    ResourceAgentRun.id == run_id,
                    ResourceAgentRun.worker_lease_token == token,
                    ResourceAgentRun.status == "running",
                )
                .with_for_update()
            )
            if run is not None:
                await _persist_stage_usage(session, run, stage_usage)
                if run.attempt_count < 5:
                    run.status = "submitted"
                    run.error = detail
                    run.worker_lease_token = None
                    run.worker_lease_expires_at = None
                    await session.commit()
                    raise RuntimeError("transient Resources failure") from exc
                run.status = "error"
                run.error = detail
                run.finished_at = datetime.now(UTC)
                run.worker_lease_token = None
                run.worker_lease_expires_at = None
                existing = await session.scalar(
                    select(AgentMessage).where(
                        AgentMessage.conversation_id == run.conversation_id,
                        AgentMessage.sequence == run.user_sequence + 1,
                    )
                )
                if existing is None:
                    session.add(
                        AgentMessage(
                            conversation_id=run.conversation_id,
                            sequence=run.user_sequence + 1,
                            role="assistant",
                            content=detail,
                            citations=[],
                            usage={"status": "error"},
                        )
                    )
                await session.commit()
        await _notify_safe(
            owner_user_id,
            "agent_run_error",
            conversation_id=str(conversation_id),
            detail=detail,
        )
        return {"status": "error"}
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
