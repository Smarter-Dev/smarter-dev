#!/usr/bin/env python3
"""Run the application's hourly content-retention jobs.

Scrubs expired Discord message content and deletes expired, short-lived agent
web-search previews. Exits 0 on success, 1 on an unhandled exception; counts go
to the log.

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
from smarter_dev.web.search_previews import delete_expired_search_previews

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("retention_sweep")


async def main() -> int:
    async with get_db_session_context() as session:
        result = await run_retention_sweep(session)
        deleted_previews = await delete_expired_search_previews(session)
        await session.commit()
    logger.info(
        "retention sweep complete: %s; search_result_previews=%d deleted",
        result,
        deleted_previews,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("retention sweep failed")
        sys.exit(1)
