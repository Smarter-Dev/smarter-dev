#!/usr/bin/env python3
"""Scrub expired Discord message content (CronJob entry point).

Runs :func:`smarter_dev.web.retention.run_retention_sweep` once: every table
that passively captures Discord message text has the human text nulled out on
rows older than the 48-hour window, keeping the row's timestamps, counters and
cost. Exits 0 on success, 1 on an unhandled exception; counts go to the log.

Intended to be triggered hourly by a Kubernetes CronJob
(``k8s/cron-retention-sweep.yaml``). Locally:
    .venv/bin/python scripts/retention_sweep.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.retention import run_retention_sweep

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("retention_sweep")


async def main() -> int:
    async with get_db_session_context() as session:
        result = await run_retention_sweep(session)
    logger.info("retention sweep complete: %s", result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("retention sweep failed")
        sys.exit(1)
