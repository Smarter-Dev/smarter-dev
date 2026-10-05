#!/usr/bin/env python3
"""Run the application's hourly content-retention jobs.

Scrubs expired Discord message content, deletes expired, short-lived agent
web-search previews, deletes Skrift worker rows past their window (7 days,
or the row's own expiry) and empties the question from finished
``work_dispatches`` rows that still hold one. Exits
0 on success, 1 on an unhandled exception; counts go to the log.

Intended to be triggered hourly by a Kubernetes CronJob
(``k8s/cron-retention-sweep.yaml``). Locally:
    .venv/bin/python scripts/retention_sweep.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.chat.dispatch import clear_finished_dispatch_payloads
from smarter_dev.web.retention import run_retention_sweep
from smarter_dev.web.search_previews import delete_expired_search_previews
from smarter_dev.web.worker_retention import delete_expired_worker_rows

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("retention_sweep")


async def main() -> int:
    async with get_db_session_context() as session:
        result = await run_retention_sweep(session)
        deleted_previews = await delete_expired_search_previews(session)
        await session.commit()
        deleted_worker_rows = await delete_expired_worker_rows(session)
        cleared_dispatches = await clear_finished_dispatch_payloads(session)
    logger.info(
        "retention sweep complete: %s; search_result_previews=%d deleted; "
        "worker rows deleted: %s; work_dispatches questions cleared=%d",
        result,
        deleted_previews,
        deleted_worker_rows,
        cleared_dispatches,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("retention sweep failed")
        sys.exit(1)
