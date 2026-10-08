"""Tests for the one-off that prices proactive Jev rows written at $0 (#101)."""

from __future__ import annotations

import sys
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import reprice_proactive_jev_usage  # noqa: E402
from smarter_dev.web.models import UsageCostRow  # noqa: E402


def _row(
    key,
    *,
    operation_type="proactive-watcher",
    model_id="jev-1.13.0",
    cost="0",
    input_tokens=924_157,
    output_tokens=20_244,
):
    return UsageCostRow(
        operation_key=key,
        product_mode="discord",
        operation_type=operation_type,
        provider_key="typesafe",
        catalog_model_key=model_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal(cost),
        overage_cost_usd=Decimal("0"),
        metered_at=datetime(2026, 10, 1, tzinfo=UTC),
        details={},
    )


async def _costs(db_session):
    rows = (await db_session.scalars(select(UsageCostRow))).all()
    return {row.operation_key: row.cost_usd for row in rows}


async def _seed(db_session):
    db_session.add_all(
        [
            _row("zero-1"),
            _row("zero-2", input_tokens=1_000_000, output_tokens=0),
            _row("already-priced", cost="0.01"),
            _row("search-ranking", operation_type="web_search_ranking"),
            _row("other-model", model_id="gemini-3.8-flash"),
        ]
    )
    await db_session.commit()


async def test_prices_only_zero_cost_proactive_jev_rows(db_session):
    await _seed(db_session)

    count, total = await reprice_proactive_jev_usage.reprice(db_session, dry_run=False)

    assert count == 2
    assert total == Decimal("0.038814594") + Decimal("0.042")
    costs = await _costs(db_session)
    # cost_usd is Numeric(14, 8), as on every ingest write.
    assert costs["zero-1"] == Decimal("0.03881459")
    assert costs["zero-2"] == Decimal("0.042")
    assert costs["already-priced"] == Decimal("0.01")
    assert costs["search-ranking"] == 0
    assert costs["other-model"] == 0


async def test_running_again_changes_nothing(db_session):
    await _seed(db_session)
    await reprice_proactive_jev_usage.reprice(db_session, dry_run=False)
    before = await _costs(db_session)

    count, total = await reprice_proactive_jev_usage.reprice(db_session, dry_run=False)

    assert (count, total) == (0, Decimal("0"))
    assert await _costs(db_session) == before


async def test_dry_run_reports_the_same_figures_and_writes_nothing(db_session):
    await _seed(db_session)

    count, total = await reprice_proactive_jev_usage.reprice(db_session, dry_run=True)

    assert count == 2
    assert total == Decimal("0.080814594")
    db_session.expire_all()
    costs = await _costs(db_session)
    assert costs["zero-1"] == 0 and costs["zero-2"] == 0
