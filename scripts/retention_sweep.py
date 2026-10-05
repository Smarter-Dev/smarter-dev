#!/usr/bin/env python3
"""Run the application's hourly content-retention jobs.

Scrubs expired Discord message content, deletes expired, short-lived agent
web-search previews, deletes Skrift worker rows past their window (7 days,
or the row's own expiry), empties the question from finished
``work_dispatches`` rows that still hold one, empties the payload and result
of finished Skrift jobs that still hold them and clears any model replies
still held in the usage rows of Chat turns that are no longer running. Exits
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
from smarter_dev.web.chat.usage import forget_stale_reply_copies
from smarter_dev.web.retention import run_retention_sweep
from smarter_dev.web.search_previews import delete_expired_search_previews
from smarter_dev.web.worker_retention import delete_expired_worker_rows
from smarter_dev.web.worker_retention import empty_finished_job_states

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
        emptied_jobs = await empty_finished_job_states(session)
        cleared_dispatches = await clear_finished_dispatch_payloads(session)
        cleared_replies = await forget_stale_reply_copies(session)
    logger.info(
        "retention sweep complete: %s; search_result_previews=%d deleted; "
        "worker rows deleted: %s; finished jobs emptied=%d; "
        "work_dispatches questions cleared=%d; "
        "chat usage rows cleared of replies=%d",
        result,
        deleted_previews,
        deleted_worker_rows,
        emptied_jobs,
        cleared_dispatches,
        cleared_replies,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("retention sweep failed")
        sys.exit(1)
