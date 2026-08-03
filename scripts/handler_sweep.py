#!/usr/bin/env python3
"""Revive recurring handler schedules whose worker-job chain has broken.

A recurring schedule is a chain of one-shot jobs, each enqueued by the previous
one's successful completion. Any single failure — a dead-lettered job, an
evicted pod, a deploy that makes every fire raise — ends the chain permanently
and silently. This sweep is the missing head pointer: it treats the handler row
as the source of truth and re-arms anything that has stopped firing.

Exits 0 on success, 1 on an unhandled exception; what it did goes to the log.

Intended to be triggered by a Kubernetes CronJob
(``k8s/cron-handler-sweep.yaml``). Locally:
    .venv/bin/python scripts/handler_sweep.py
    .venv/bin/python scripts/handler_sweep.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from smarter_dev.shared.database import get_db_session_context, get_session_maker
from smarter_dev.web.handler_sweep import find_stalled_chains, sweep_schedule_chains

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("handler_sweep")


def _configure_worker_runtime() -> None:
    """Configure the worker runtime so ``submit()`` can reach the shared queue.

    A standalone process gets no runtime from ASGI startup, and Skrift has no
    public one-call bootstrap for scripts, so this reproduces what
    ``skrift workers run`` does with the public ``configure_workers`` API:
    resolve backends from the app's own worker settings (the ``distributed``
    preset -> Redis queue + Redis state store, SQLAlchemy dead-letter/archive)
    so this process enqueues onto exactly the queue the agent-worker drains.

    ``out_of_process`` configures the runtime WITHOUT starting a consumer pool —
    this script only ever submits, it never executes a job.
    """
    from skrift.config import get_settings as get_skrift_settings
    from skrift.workers import configure_workers

    skrift_settings = get_skrift_settings()
    configure_workers(
        mode="out_of_process",
        queues=tuple(skrift_settings.workers.queues),
        visibility_timeout=skrift_settings.workers.visibility_timeout,
        terminal_job_state_ttl=(
            skrift_settings.workers.retention.terminal_job_state_ttl
        ),
        backend_imports=skrift_settings.workers.backends,
        settings=skrift_settings,
        session_maker=get_session_maker(),
    )


async def main(dry_run: bool) -> int:
    if dry_run:
        async with get_db_session_context() as session:
            stalled = await find_stalled_chains(session)
        if not stalled:
            logger.info("no stalled schedule chains")
            return 0
        for chain in stalled:
            logger.warning(
                "STALLED %s: last fired %s, overdue by %s, settings=%s",
                chain.label,
                chain.last_fired_at,
                chain.overdue_by,
                chain.settings,
            )
        logger.info("%d stalled chain(s); re-run without --dry-run to revive", len(stalled))
        return 0

    _configure_worker_runtime()
    async with get_db_session_context() as session:
        result = await sweep_schedule_chains(session)
    logger.info(
        "handler schedule sweep complete: stalled=%d rearmed=%d failed=%d %s",
        result["stalled"],
        len(result["rearmed"]),
        len(result["failed"]),
        result,
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report stalled chains without re-arming anything",
    )
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args.dry_run)))
    except Exception:
        logger.exception("handler schedule sweep failed")
        sys.exit(1)
