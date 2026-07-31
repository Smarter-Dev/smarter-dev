"""Provider-request metering, durable history codecs, and cancellation seams."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import PartStartEvent
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models import StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.chat.entitlements import resolve_spend_tier
from smarter_dev.web.chat.limits import OperationAlreadyReserved
from smarter_dev.web.chat.limits import current_spend_decision
from smarter_dev.web.chat.limits import reserve_operation
from smarter_dev.web.chat.limits import settle_reservation
from smarter_dev.web.chat.usage import record_settled_chat_usage
from smarter_dev.web.llm_pricing import price_rates_for_model
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatSubagent
from smarter_dev.web.models import WebChatTurn

logger = logging.getLogger(__name__)


class HardSpendCutoff(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    """Durable user cancellation, distinct from infrastructure task cancel."""


class RunSuperseded(asyncio.CancelledError):
    pass


def encode_model_messages(messages: list[ModelMessage]) -> list[dict]:
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


def decode_model_messages(payload: list[dict] | None) -> list[ModelMessage]:
    if not payload:
        return []
    return list(ModelMessagesTypeAdapter.validate_python(payload))


def _usage_parts(response: ModelResponse) -> tuple[int, int, int, int]:
    usage = response.usage
    details = getattr(usage, "details", {}) or {}
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        int(
            getattr(usage, "cache_read_tokens", 0)
            or details.get("cache_read_tokens", 0)
            or 0
        ),
        int(
            getattr(usage, "cache_write_tokens", 0)
            or details.get("cache_write_tokens", 0)
            or 0
        ),
    )


@dataclass(slots=True)
class MeteringContext:
    model: Any
    owner_id: UUID
    conversation_id: UUID
    turn_id: UUID
    intelligence_mode: str
    operation_type: str
    operation_prefix: str
    reasoning_level: str | None = None
    subagent_id: UUID | None = None
    worker_lease_token: str | None = None
    subagent_lease_fence: int | None = None
    allow_hard_cutoff: bool = False


class _ReplayStream(StreamedResponse):
    """Present a settled durable response through Pydantic's stream contract."""

    def __init__(self, response: ModelResponse, parameters: ModelRequestParameters):
        super().__init__(parameters)
        self.response = response
        self._usage = response.usage

    async def _get_event_iterator(self):
        for index, part in enumerate(self.response.parts):
            yield PartStartEvent(index=index, part=part)

    async def close_stream(self) -> None:
        return None

    def get(self) -> ModelResponse:
        return self.response

    @property
    def model_name(self) -> str:
        return self.response.model_name or "replayed"

    @property
    def provider_name(self) -> str | None:
        return self.response.provider_name

    @property
    def provider_url(self) -> str | None:
        return self.response.provider_url

    @property
    def timestamp(self):
        return self.response.timestamp


class SpendMeteredModel(WrapperModel):
    """Reserve and settle every provider request, including tool-loop requests."""

    def __init__(self, wrapped, context: MeteringContext):
        super().__init__(wrapped)
        self.context = context
        self.request_ordinal = 0

    def _next_key(self) -> str:
        self.request_ordinal += 1
        # A replacement worker must never collapse a second, potentially billed
        # provider attempt into the stale worker's usage row. The durable lease
        # or child fence identifies the execution attempt while ordinals remain
        # stable within that attempt for replay.
        execution = self.context.worker_lease_token or (
            str(self.context.subagent_lease_fence)
            if self.context.subagent_lease_fence is not None
            else "unleased"
        )
        return (
            f"{self.context.operation_prefix}:execution:{execution}:"
            f"request:{self.request_ordinal}"
        )[:200]

    async def _replay(self, operation_key: str) -> ModelResponse | None:
        async with get_db_session_context() as session:
            row = await session.scalar(
                select(UsageCostRow).where(UsageCostRow.operation_key == operation_key)
            )
            payload = (
                (row.details or {}).get("model_response")
                if row and (row.details or {}).get("response_complete") is True
                else None
            )
        if not payload:
            return None
        messages = decode_model_messages([payload])
        return (
            messages[0] if messages and isinstance(messages[0], ModelResponse) else None
        )

    async def _reserve(
        self,
        operation_key: str,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> None:
        replay = await self._replay(operation_key)
        if replay is not None:
            return
        async with get_db_session_context() as session:
            prior = await session.scalar(
                select(UsageCostRow).where(UsageCostRow.operation_key == operation_key)
            )
            if prior is not None:
                raise HardSpendCutoff(
                    "A prior incomplete provider response already incurred usage"
                )
        try:
            counted = await self.wrapped.count_tokens(
                messages, model_settings, model_request_parameters
            )
            input_tokens = int(getattr(counted, "input_tokens", 0) or 0)
        except Exception:
            input_tokens = max(len(repr(messages)) // 4, 1)
        rates = price_rates_for_model(self.context.model)
        if rates is None:
            raise HardSpendCutoff("pricing unavailable for selected model")
        configured_output = int((model_settings or {}).get("max_tokens") or 0)
        max_output = min(
            configured_output or self.context.model.max_output_tokens,
            self.context.model.max_output_tokens,
        )
        estimate = (
            Decimal(min(input_tokens, self.context.model.context_window))
            * rates.input_mtok
            + Decimal(max_output) * rates.output_mtok
        ) / Decimal("1000000")
        async with get_db_session_context() as session:
            from skrift.auth.services import get_user_permissions
            from skrift.db.models.user import User

            user = await session.get(User, self.context.owner_id)
            if user is None or not user.is_active:
                raise HardSpendCutoff("Chat account is inactive")
            turn = await session.get(WebChatTurn, self.context.turn_id)
            if turn is None:
                raise RunSuperseded()
            if turn.cutoff_at is not None and not self.context.allow_hard_cutoff:
                raise HardSpendCutoff("Chat hard spend cutoff reached")
            if (
                self.context.worker_lease_token is not None
                and turn.worker_lease_token != self.context.worker_lease_token
            ):
                raise RunSuperseded()
            if self.context.subagent_id is not None:
                child = await session.get(WebChatSubagent, self.context.subagent_id)
                if (
                    child is None
                    or child.status != "running"
                    or child.lease_fence != self.context.subagent_lease_fence
                ):
                    raise RunSuperseded()
            permissions = await get_user_permissions(session, self.context.owner_id)
            tier = resolve_spend_tier(permissions)
            if tier is None:
                raise HardSpendCutoff("Chat entitlement is no longer available")
            try:
                reservation, _decision = await reserve_operation(
                    session,
                    operation_key=operation_key,
                    operation_type=self.context.operation_type,
                    user_id=self.context.owner_id,
                    tier=tier,
                    intelligence_mode=self.context.intelligence_mode,
                    estimate_usd=estimate,
                    conversation_id=self.context.conversation_id,
                    root_turn_id=self.context.turn_id,
                    window_id=turn.four_hour_window_id,
                    allow_hard_cutoff=self.context.allow_hard_cutoff,
                )
            except OperationAlreadyReserved as exc:
                raise HardSpendCutoff(
                    "A prior provider attempt is still pending settlement"
                ) from exc
            if reservation is None:
                turn = await session.get(WebChatTurn, self.context.turn_id)
                if turn is not None and turn.cutoff_at is None:
                    from datetime import UTC
                    from datetime import datetime

                    turn.cutoff_at = datetime.now(UTC)
                await session.commit()
                raise HardSpendCutoff("Chat hard spend cutoff reached")
            await session.commit()

    async def _settle(
        self,
        operation_key: str,
        response: ModelResponse,
        _request_messages: list[ModelMessage],
        *,
        response_complete: bool = True,
    ) -> None:
        i, o, cr, cw = _usage_parts(response)
        async with get_db_session_context() as session:
            from skrift.auth.services import get_user_permissions

            permissions = await get_user_permissions(session, self.context.owner_id)
            tier = resolve_spend_tier(permissions)
            if tier is None:
                tier = "hacker"  # incurred usage still must be billed durably
            turn = await session.get(WebChatTurn, self.context.turn_id)
            child = (
                await session.get(WebChatSubagent, self.context.subagent_id)
                if self.context.subagent_id is not None
                else None
            )
            lease_current = bool(
                turn
                and (
                    self.context.worker_lease_token is None
                    or turn.worker_lease_token == self.context.worker_lease_token
                )
                and (
                    child is None
                    or child.lease_fence == self.context.subagent_lease_fence
                )
            )
            await record_settled_chat_usage(
                session,
                operation_key=operation_key,
                operation_type=self.context.operation_type,
                model=self.context.model,
                tier=tier,
                input_tokens=i,
                output_tokens=o,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                user_id=self.context.owner_id,
                conversation_id=self.context.conversation_id,
                root_turn_id=self.context.turn_id,
                subagent_id=self.context.subagent_id,
                reasoning_level=self.context.reasoning_level,
                intelligence_mode=self.context.intelligence_mode,
                details={
                    "model_response": encode_model_messages([response])[0],
                    # Keep only the provider response in the usage ledger.
                    # The turn worker rebuilds the user request from its durable
                    # message/attachments when recovering, avoiding duplicated
                    # multi-megabyte image payloads in accounting metadata.
                    "durable_delta": encode_model_messages([response]),
                    "request_ordinal": self.request_ordinal,
                    "response_complete": response_complete,
                    "lease_current_at_settlement": lease_current,
                },
            )
            decision = await current_spend_decision(
                session,
                user_id=self.context.owner_id,
                tier=tier,
                intelligence_mode=self.context.intelligence_mode,
                window_id=turn.four_hour_window_id if turn is not None else None,
            )
            if decision.hard_cutoff and turn is not None and turn.cutoff_at is None:
                from datetime import UTC
                from datetime import datetime

                turn.cutoff_at = datetime.now(UTC)
            conversation = await session.get(
                WebChatConversation, self.context.conversation_id
            )
            if (
                conversation is not None
                and self.context.operation_type == "primary"
                and lease_current
            ):
                # A single response usage is the current request context, unlike
                # aggregate AgentRunResult usage across a tool loop.
                conversation.current_context_tokens = i
            await session.commit()
        try:
            from skrift.notifications import notify_user

            await notify_user(
                str(self.context.owner_id),
                "chat_usage_updated",
                conversation_id=str(self.context.conversation_id),
                turn_id=str(self.context.turn_id),
            )
        except Exception:
            pass

    async def _release(self, operation_key: str) -> None:
        async with get_db_session_context() as session:
            await settle_reservation(session, operation_key, status="released")
            await session.commit()

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        operation_key = self._next_key()
        replay = await self._replay(operation_key)
        if replay is not None:
            return replay
        await self._reserve(
            operation_key, messages, model_settings, model_request_parameters
        )
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except BaseException:
            # Provider acceptance is ambiguous on transport errors/timeouts.
            # Keep the bounded reservation active until expiry rather than
            # incorrectly restoring spend capacity for a potentially billed call.
            raise
        await self._settle(operation_key, response, messages)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ):
        operation_key = self._next_key()
        replay = await self._replay(operation_key)
        if replay is not None:
            yield _ReplayStream(replay, model_request_parameters)
            return
        await self._reserve(
            operation_key, messages, model_settings, model_request_parameters
        )
        stream = None
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
                response = stream.get()
        except BaseException:
            # Streaming cancellation can still have incurred provider usage.
            # Persist whatever the provider exposed instead of silently
            # releasing it as free.
            partial = None
            with suppress(Exception):
                partial = stream.get() if stream is not None else None
            if partial is not None and sum(_usage_parts(partial)) > 0:
                await self._settle(
                    operation_key, partial, messages, response_complete=False
                )
            # Without provider usage the request may still have been accepted;
            # retain its reservation until expiry to fail closed.
            raise
        await self._settle(operation_key, response, messages)


async def wait_for_cancellation(
    *,
    turn_id: UUID,
    subagent_id: UUID | None = None,
    worker_lease_token: str | None = None,
    allow_cutoff: bool = False,
    interval: float = 1.0,
) -> None:
    while True:
        try:
            async with get_db_session_context() as session:
                turn = await session.get(WebChatTurn, turn_id)
                child = (
                    await session.get(WebChatSubagent, subagent_id)
                    if subagent_id is not None
                    else None
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cancellation is advisory until the durable row can be read. A
            # transient database disconnect must not abort a live provider run
            # or cause the queue to retry and duplicate the entire agent loop.
            logger.warning(
                "Chat cancellation check temporarily unavailable for turn %s",
                turn_id,
                exc_info=True,
            )
            await asyncio.sleep(interval)
            continue
        if turn is None or turn.stop_requested_at is not None:
            raise RunCancelled()
        if turn.cutoff_at is not None and not allow_cutoff:
            raise HardSpendCutoff("Chat hard spend cutoff reached")
        if (
            worker_lease_token is not None
            and turn.worker_lease_token != worker_lease_token
        ):
            raise RunSuperseded()
        if child is not None and child.cancel_requested_at is not None:
            raise RunCancelled()
        await asyncio.sleep(interval)


async def run_cancellable(
    awaitable,
    *,
    turn_id: UUID,
    subagent_id: UUID | None = None,
    worker_lease_token: str | None = None,
    allow_cutoff: bool = False,
):
    work = asyncio.create_task(awaitable)
    cancel_watch = asyncio.create_task(
        wait_for_cancellation(
            turn_id=turn_id,
            subagent_id=subagent_id,
            worker_lease_token=worker_lease_token,
            allow_cutoff=allow_cutoff,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {work, cancel_watch},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_watch in done:
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
            await cancel_watch
        return await work
    finally:
        cancel_watch.cancel()
        with suppress(asyncio.CancelledError):
            await cancel_watch
