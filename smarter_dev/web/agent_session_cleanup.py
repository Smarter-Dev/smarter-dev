"""Delete a Skrift agent session's stored copy once its caller has the result.

A Skrift agent run (the Resources stages, the chat-title agent, the blogging
stages) keeps its own copy of everything it saw and said in the worker tables:
the session's run state (``runstate:<id>`` in ``worker_state``) holds the
prompt, any earlier messages passed in, every tool call and its result and the
output; ``worker_archive_snapshots`` holds copies of that state; and the
session's event stream (``agents:run:<id>`` in ``worker_events``) records the
same as events. Skrift would keep them until the hourly retention job deleted
them, 5 hours after the session last changed (gone by 6 hours, as the job
runs hourly).

The caller is the only reader: it awaits the session's result and its usage,
writes what the product needs to its own tables, and never reads the session
again. So once it has, it deletes all of it here. Nothing here runs a session
again either: a Resources retry, the only retry of an agent run, starts fresh
sessions.

The job that ran a session keeps no content either: its state is emptied as it
finishes (:mod:`smarter_dev.web.worker_state_store`). A failure here is logged
and left to the hourly retention job, which deletes a session 5 hours after its
last write.

A session's run state is also the only record of its token usage. A caller
that does not record usage itself passes ``usage`` so each model turn becomes a
``usage_cost_rows`` row, in the same transaction, before the session goes.
Skrift sums a turn's usage across its requests, so a turn is priced from its
stored responses, each on its own (``turn_billing``): a long-context tier is
decided per request. A failed run's responses, which Skrift keeps for a turn
that dies but never adds to its usage, were billed too and are added.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from skrift.agents.models import AgentUsageRecord
from skrift.agents.models import RunState
from skrift.db.models.worker import WorkerArchiveEventRecord
from skrift.db.models.worker import WorkerArchiveSnapshotRecord
from skrift.db.models.worker import WorkerEventRecord
from skrift.db.models.worker import WorkerStateRecord
from sqlalchemy import delete
from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.shared.request_usage import response_request_tokens

logger = logging.getLogger(__name__)

_RUNSTATE_PREFIX = "runstate:"
_RUN_STREAM_PREFIX = "agents:run:"


@dataclass(frozen=True)
class UsageOwner:
    """Who a session's model turns are charged to, as ``usage_cost_rows``."""

    product_mode: str
    operation_type: str
    user_id: UUID | None = None
    conversation_id: UUID | None = None


def _catalog_model(configured: str | None):
    if not configured:
        return None
    wire = configured.split(":", 1)[-1]
    return next(
        (
            model
            for model in MODEL_CATALOG
            if wire == model.model_id or wire.startswith(model.model_id)
        ),
        None,
    )


def turn_responses(state: RunState) -> dict[str, list[dict]]:
    """Each turn's stored model responses (JSON), by turn id.

    A turn's messages follow the user message carrying its ``turn_id`` up to
    the next one's; a steer (a user message with no turn id) stays in its turn.
    """
    by_turn: dict[str, list[dict]] = {}
    turn_id = None
    for message in state.messages:
        if message.get("role") == "user":
            turn_id = message.get("turn_id") or turn_id
        elif message.get("role") == "model" and turn_id is not None:
            content = message.get("content")
            if isinstance(content, dict) and content.get("kind") == "response":
                by_turn.setdefault(turn_id, []).append(content)
    return by_turn


@dataclass(frozen=True)
class TurnBilling:
    """What a turn was billed: its tokens and their cost."""

    tokens: RequestTokens
    cost: Decimal
    # The model the session ran, from a usage record or else its responses.
    model_name: str | None = None


def _recorded_prefix(total: RequestTokens, requests: list[RequestTokens]) -> int | None:
    """How many of *requests*, from the first, add up to exactly *total*;
    None when no run of them does."""
    running = RequestTokens()
    if running == total:
        return 0
    for index, request in enumerate(requests, 1):
        running = RequestTokens(*(a + b for a, b in zip(running, request)))
        if running == total:
            return index
    return None


def turn_billing(
    model: CatalogModel, usage: AgentUsageRecord | None, responses: list[dict]
) -> TurnBilling:
    """A turn's tokens and cost, each of its stored responses priced on its own.

    Skrift's record is the total of the turn's runs that reached a pause or
    the end. A run that failed is not in it, yet a turn that then dies keeps
    that run's responses after the recorded ones, and the provider billed
    them: they are added, priced on their own too. Tokens the stored
    responses do not account for are priced at the base rate
    (``calc_cost_by_request``); when the record is not the sum of the first
    responses, the record alone is priced, as before. A Skrift agent runs one
    model, so every response is that model's.
    """
    from smarter_dev.web.chat.usage import usage_cost_by_request

    total = (
        RequestTokens(
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        if usage is not None
        else RequestTokens()
    )
    requests = response_request_tokens(responses)
    recorded = _recorded_prefix(total, requests)
    if recorded is None:
        return TurnBilling(total, usage_cost_by_request(model, total, requests))
    unrecorded = requests[recorded:]
    if not unrecorded:
        return TurnBilling(total, usage_cost_by_request(model, total, requests))
    extra = RequestTokens(*(sum(column) for column in zip(*unrecorded)))
    return TurnBilling(
        RequestTokens(*(a + b for a, b in zip(total, extra))),
        usage_cost_by_request(model, total, requests[:recorded])
        + usage_cost_by_request(model, extra, unrecorded),
    )


def turn_cost(
    model: CatalogModel, usage: AgentUsageRecord | None, responses: list[dict]
) -> Decimal:
    """``turn_billing``'s cost."""
    return turn_billing(model, usage, responses).cost


def _billed_turns(state: RunState):
    """``(turn id, its record or None, its model or None, its responses)`` for
    every turn with a usage record or stored responses: a turn whose only run
    failed has no record, but its responses were billed."""
    responses = turn_responses(state)
    configured = next(
        (
            usage.configured_model or usage.model_name
            for usage in state.turn_usage.values()
            if usage.configured_model or usage.model_name
        ),
        None,
    )
    for turn_id in [*state.turn_usage, *(t for t in responses if t not in state.turn_usage)]:
        usage = state.turn_usage.get(turn_id)
        turn = responses.get(turn_id, [])
        if usage is not None:
            name = usage.configured_model or usage.model_name
        else:
            name = next(
                (r.get("model_name") for r in turn if r.get("model_name")), configured
            )
        yield turn_id, usage, name, turn


def session_billing(state: RunState) -> TurnBilling | None:
    """Every turn's ``turn_billing`` added up, or None when a turn's model has
    no catalog price."""
    tokens, cost = RequestTokens(), Decimal("0")
    billed_model = None
    for _turn_id, usage, name, responses in _billed_turns(state):
        model = _catalog_model(name)
        if model is None:
            return None
        billed = turn_billing(model, usage, responses)
        tokens = RequestTokens(*(a + b for a, b in zip(tokens, billed.tokens)))
        cost += billed.cost
        billed_model = billed_model or name
    return TurnBilling(tokens, cost, billed_model)


def session_cost(state: RunState) -> Decimal | None:
    """``session_billing``'s cost."""
    billed = session_billing(state)
    return billed.cost if billed is not None else None


async def _record_usage(session, state_keys: list[str], owner: UsageOwner) -> None:
    """One cost row per model turn, keyed by session and turn so a re-run adds none."""
    from smarter_dev.web.chat.usage import record_usage

    values = (
        await session.scalars(
            select(WorkerStateRecord.value).where(WorkerStateRecord.key.in_(state_keys))
        )
    ).all()
    for value in values:
        state = RunState.model_validate(value.get("value", value))
        for turn_id, usage, name, responses in _billed_turns(state):
            model = _catalog_model(name)
            if model is None:
                logger.warning(
                    "Agent session %s: no catalog price for %r; its usage is not recorded",
                    state.session_id,
                    name,
                )
                continue
            billed = turn_billing(model, usage, responses)
            if not any(billed.tokens):
                continue
            await record_usage(
                session,
                operation_key=f"skrift:{state.session_id}:{turn_id}"[:200],
                product_mode=owner.product_mode,
                operation_type=owner.operation_type,
                model=model,
                input_tokens=billed.tokens.input_tokens,
                output_tokens=billed.tokens.output_tokens,
                cache_read_tokens=billed.tokens.cache_read_tokens,
                cache_write_tokens=billed.tokens.cache_write_tokens,
                cost_usd=billed.cost,
                user_id=owner.user_id,
                conversation_id=owner.conversation_id,
                details={"agent": state.agent_name}
                | ({} if usage is not None else {"failed": True}),
            )


async def _sessions_rooted_at(session, root_session_ids: list[str]) -> set[str]:
    """Every session whose root is one of these: a sub-agent records its root."""
    rows = await session.execute(
        select(
            WorkerStateRecord.key,
            WorkerStateRecord.value[("value", "root_session_id")].as_string(),
        ).where(WorkerStateRecord.key.startswith(_RUNSTATE_PREFIX))
    )
    roots = set(root_session_ids)
    return {
        key.removeprefix(_RUNSTATE_PREFIX) for key, root in rows.all() if root in roots
    }


async def forget_agent_sessions(
    session_ids: Iterable[str],
    *,
    with_sub_agents: bool = False,
    keep_events: bool = False,
    usage: UsageOwner | None = None,
) -> None:
    """Delete the sessions' run state, snapshots and, unless kept, event streams.

    ``with_sub_agents`` also deletes every session dispatched under them, found
    by its root session. ``keep_events`` is for sessions whose event streams a
    page still shows (the blogging pipeline's run timeline); those streams go
    with the hourly retention job instead.
    """
    ids = sorted({session_id for session_id in session_ids if session_id})
    if not ids:
        return
    try:
        async with get_db_session_context() as session:
            if with_sub_agents:
                ids = sorted(set(ids) | await _sessions_rooted_at(session, ids))
            state_keys = [_RUNSTATE_PREFIX + session_id for session_id in ids]
            streams = [_RUN_STREAM_PREFIX + session_id for session_id in ids]
            if usage is not None:
                await _record_usage(session, state_keys, usage)
            await session.execute(
                delete(WorkerStateRecord).where(WorkerStateRecord.key.in_(state_keys))
            )
            await session.execute(
                delete(WorkerArchiveSnapshotRecord).where(
                    WorkerArchiveSnapshotRecord.key.in_(state_keys)
                )
            )
            if not keep_events:
                await session.execute(
                    delete(WorkerEventRecord).where(
                        WorkerEventRecord.stream.in_(streams)
                    )
                )
                await session.execute(
                    delete(WorkerArchiveEventRecord).where(
                        WorkerArchiveEventRecord.stream.in_(streams)
                    )
                )
            await session.commit()
    except Exception:
        logger.exception(
            "Could not delete %d finished agent session(s); the hourly retention job will",
            len(ids),
        )
