"""Usage-ledger rows for the bot's and the site's own model runs.

The Discord bot's side agents (summarizers, the utility agents, the rule
matcher, handler authoring, privacy purges), its memory jobs and the handler
runtime are nobody's Chat spend: their rows are ``product_mode="discord"``
under their own ``operation_type``, the way the proactive, compaction and web
search rows are, so no user's Chat bill or four-hour window ever counts them.
The blogging pipeline's rows are the site's own, under ``"resources"`` (as the
resource-title agent's are) with no conversation, so the Resources question
count never counts them either.

Each model a run used is one row, priced per provider request
(``calc_cost_by_request``): a long-context tier is decided per request. Rows
are keyed by the caller's operation key and the model, so reporting the same
run again writes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.shared.usage_meter import UsageMeter
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import UsageCostRow

logger = logging.getLogger(__name__)

# (model, totals, each request's counts), as ``UsageMeter.stages`` gives them.
Stage = tuple[str, RequestTokens, list[RequestTokens]]

# usage_cost_rows.operation_type is String(40).
OPERATION_TYPE_MAX = 40
_OPERATION_KEY_MAX = 200


def stage_operation_key(operation_key: str, model_name: str) -> str:
    return f"{operation_key}:{model_name}"[:_OPERATION_KEY_MAX]


async def record_stages(
    session: AsyncSession,
    stages: Iterable[Stage],
    *,
    operation_key: str,
    product_mode: str,
    operation_type: str,
    user_id: UUID | None = None,
    discord_user_id: str | None = None,
    conversation_id: UUID | None = None,
    root_turn_id: UUID | None = None,
    reasoning_level: str | None = None,
    metered_at: datetime | None = None,
    details: dict | None = None,
) -> int:
    """Add one row per model in *stages*; returns the rows added.

    Rows already written under the same keys are left as they are. The caller
    commits.
    """
    from smarter_dev.web.api_native.chat_conversations import _normalized_model_identity

    if len(operation_type) > OPERATION_TYPE_MAX:
        raise ValueError(
            f"operation_type longer than {OPERATION_TYPE_MAX}: {operation_type!r}"
        )
    added = 0
    for model_name, total, requests in stages:
        if not any(total):
            continue
        key = stage_operation_key(operation_key, model_name)
        if await session.scalar(
            select(UsageCostRow.id).where(UsageCostRow.operation_key == key)
        ):
            continue
        provider, catalog_key, wire_id = _normalized_model_identity(model_name)
        row = UsageCostRow(
            operation_key=key,
            product_mode=product_mode,
            operation_type=operation_type,
            user_id=user_id,
            discord_user_id=discord_user_id,
            conversation_id=conversation_id,
            root_turn_id=root_turn_id,
            provider_key=provider,
            catalog_model_key=catalog_key,
            model_id=wire_id,
            reasoning_level=reasoning_level,
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
            cache_read_tokens=total.cache_read_tokens,
            cache_write_tokens=total.cache_write_tokens,
            cost_usd=calc_cost_by_request(model_name, total, requests),
            overage_cost_usd=Decimal("0"),
            metered_at=metered_at or datetime.now(UTC),
            details={**(details or {}), "requests": len(requests)},
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            # Written by a concurrent report of the same run.
            continue
        added += 1
    return added


async def record_meter(session: AsyncSession, meter: UsageMeter, **fields) -> int:
    """``record_stages`` for everything *meter* saw."""
    return await record_stages(session, meter.stages(), **fields)


async def record_meter_now(meter: UsageMeter, *, session_factory=None, **fields) -> int:
    """``record_meter`` in its own session and transaction.

    For callers with no session of their own, or whose session may roll back
    (a failed run's usage is still owed). ``session_factory`` is the caller's
    own, when it was handed one (a job's or a test's); the main database's by
    default. Never raises: a lost row is logged.
    """
    from smarter_dev.shared.database import get_db_session_context

    if not meter:
        return 0
    try:
        async with (session_factory or get_db_session_context)() as session:
            added = await record_meter(session, meter, **fields)
            await session.commit()
            return added
    except Exception:
        logger.exception(
            "Could not record %s usage (%s)",
            fields.get("operation_type"),
            fields.get("operation_key"),
        )
        return 0
