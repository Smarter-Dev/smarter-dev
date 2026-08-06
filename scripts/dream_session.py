#!/usr/bin/env python3
"""Nightly chat-memory dream session (CronJob entry point).

Runs one dream per guild that needs one for the day that just ended, logs a
line per guild, and exits 1 if any guild failed — a failed guild kept its
notes, so the Job's retry (and, failing that, tomorrow's run) picks it up
again. Guilds that were skipped or that kept yesterday's blob are not
failures: nothing was lost and nothing needs re-running.

Intended to be triggered by a Kubernetes CronJob. Locally:
    uv run python scripts/dream_session.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC
from datetime import datetime

from smarter_dev.web.chat_memory_dream import run_dream_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dream_session")


async def main() -> int:
    summary = await run_dream_session(datetime.now(UTC))
    for result in summary.results:
        logger.info(
            "guild %s: %s (notes=%d, revision=%s)",
            result.guild_id,
            result.outcome.value,
            result.notes_consumed,
            result.revision,
        )
    for guild_id in summary.failed_guild_ids:
        logger.error("guild %s: dream failed; its notes wait for tomorrow", guild_id)
    logger.info(
        "dream session finished: %d dreamed/skipped, %d failed",
        len(summary.results),
        len(summary.failed_guild_ids),
    )
    return 1 if summary.failed_guild_ids else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("dream session failed")
        sys.exit(1)
