"""The web search's spend in the usage ledger, which the admin invoices read.

Each attempt writes one row per paid step: Luna's queries, the Brave requests
that were answered, and Jev's ranking. Rows are keyed by search and attempt,
so a retried job records what the retry spent and a repeated write records
nothing."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search import brave

logger = logging.getLogger(__name__)

PRODUCT_MODE = "search"


def _operation_key(run: WebSearchRun, step: str) -> str:
    return f"web_search:{run.id}:attempt:{run.attempt_count}:{step}"


async def _append(session, row: UsageCostRow) -> None:
    existing = await session.scalar(
        select(UsageCostRow.id).where(UsageCostRow.operation_key == row.operation_key)
    )
    if existing is None:
        session.add(row)


def _row(run: WebSearchRun, step: str, **fields) -> UsageCostRow:
    return UsageCostRow(
        operation_key=_operation_key(run, step),
        product_mode=PRODUCT_MODE,
        operation_type=f"web_search_{step}",
        user_id=run.owner_user_id,
        root_turn_id=run.id,
        details={"web_search_id": str(run.id)},
        **fields,
    )


def luna_row(run: WebSearchRun, usage: dict) -> UsageCostRow | None:
    from smarter_dev.web.chat.usage import usage_cost

    wire = usage.get("model", "")
    model = next((m for m in MODEL_CATALOG if wire == m.model_id), None)
    if model is None:
        logger.warning("Web search %s: no catalog price for query model %r", run.id, wire)
        return None
    tokens = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
        "cache_write_tokens": 0,
    }
    return _row(
        run,
        "queries",
        provider_key=model.provider.value,
        catalog_model_key=model.key,
        model_id=model.model_id,
        reasoning_level="medium",
        cost_usd=usage_cost(model, *tokens.values()),
        **tokens,
    )


def brave_row(run: WebSearchRun, requests: int) -> UsageCostRow:
    row = _row(
        run,
        "brave",
        provider_key="brave",
        catalog_model_key="brave-web-search",
        model_id="brave-web-search",
        cost_usd=Decimal(brave.PRICE_PER_REQUEST_USD) * requests,
    )
    row.details = {**row.details, "requests": requests}
    return row


def jev_row(run: WebSearchRun, usage: dict) -> UsageCostRow:
    model = usage.get("model") or "jev"
    return _row(
        run,
        "ranking",
        provider_key="typesafe",
        catalog_model_key=model,
        model_id=model,
        input_tokens=int(usage.get("input_tokens") or 0),
        cost_usd=Decimal(str(usage.get("cost_usd") or 0)),
    )


async def record(run_id, rows) -> None:
    """Append the rows the ``rows(run)`` callback builds for the saved run.

    Metering never fails the search: a ledger error is logged and dropped."""
    try:
        async with get_db_session_context() as session:
            run = await session.get(WebSearchRun, run_id)
            for row in rows(run):
                if row is not None:
                    await _append(session, row)
            await session.commit()
    except Exception:  # noqa: BLE001 - the user's results matter more than the ledger row
        logger.exception("Web search %s: couldn't record usage", run_id)
