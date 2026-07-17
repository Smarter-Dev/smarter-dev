#!/usr/bin/env python3
"""Daily sudo membership sweep (CronJob entry point).

Runs ``run_daily_sweep`` once and exits with the count of expirations
and drift-restored converges in the process exit code? No — exits 0 on
success, 1 on unhandled exception. Counts go to logs.

Intended to be triggered by a Kubernetes CronJob. Locally:
    .venv/bin/python scripts/sudo_daily_sweep.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.billing.webhooks import run_daily_sweep

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sudo_daily_sweep")


async def main() -> int:
    async with get_db_session_context() as session:
        summary = await run_daily_sweep(session)
    logger.info("sweep summary: %s", summary)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("sudo daily sweep failed")
        sys.exit(1)
