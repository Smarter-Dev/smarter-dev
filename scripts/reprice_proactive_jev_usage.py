#!/usr/bin/env python3
"""One-off: price the proactive watcher's Jev usage rows that were billed at $0.

Until #101, ``llm_pricing`` had no TypeSafe entry, so every proactive wake that
ran on Jev wrote its ``usage_cost_rows`` row with ``cost_usd = 0``. This
re-prices exactly those rows (``operation_type LIKE 'proactive-%'``,
``model_id = 'jev-1.13.0'``, ``cost_usd = 0``) from their stored tokens with
``calc_cost``, the same call the ingest path now makes. A priced row no longer
matches, so running it again changes nothing.

Logs the number of rows and the total it adds, never row contents. Pass
``--dry-run`` to log the same figures and roll back. Run it once, after the
deploy that carries the TypeSafe price, as the Job in
``k8s/oneoff-reprice-proactive-jev.yaml``. Locally:
    .venv/bin/python scripts/reprice_proactive_jev_usage.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.llm_pricing import JEV_MODEL_ID
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.models import UsageCostRow

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("reprice_proactive_jev_usage")


async def reprice(session: AsyncSession, *, dry_run: bool) -> tuple[int, Decimal]:
    """Price the $0 proactive Jev rows; return how many and the total added."""
    rows = (
        await session.scalars(
            select(UsageCostRow).where(
                UsageCostRow.operation_type.like("proactive-%"),
                UsageCostRow.model_id == JEV_MODEL_ID,
                UsageCostRow.cost_usd == 0,
            )
        )
    ).all()
    total = Decimal("0")
    for row in rows:
        cost = calc_cost(
            row.input_tokens,
            row.output_tokens,
            row.model_id,
            row.cache_read_tokens,
            row.cache_write_tokens,
        )
        row.cost_usd = cost
        total += cost
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return len(rows), total


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="log what would change and roll back"
    )
    args = parser.parse_args(argv)
    async with get_db_session_context() as session:
        count, total = await reprice(session, dry_run=args.dry_run)
    logger.info(
        "%s %d proactive %s rows; total added $%s",
        "would re-price" if args.dry_run else "re-priced",
        count,
        JEV_MODEL_ID,
        total,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("re-pricing failed")
        sys.exit(1)
