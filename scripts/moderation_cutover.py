"""Switch one guild's moderation between the legacy actors and the coordinator.

    status   --guild <id>               read only: owner, generation, every
                                        legacy switch and handler row, and
                                        whether a rollback is saved
    switch   --guild <id> --by <who>    hand the guild to the coordinator and
                                        turn the legacy actors off, in one
                                        transaction (refused if already done)
    rollback --guild <id> --by <who>    restore exactly what the switch saved
                                        (refused if nothing is saved)

Run in the cluster as a Job (``k8s/oneoff-moderation-cutover.yaml``). The
logic and its guarantees are in ``smarter_dev/web/moderation_cutover.py``.
Exit status: 0 done, 2 refused (nothing changed), 1 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from sqlalchemy import text

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web import moderation_cutover as cutover

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("moderation_cutover")
#: The bot's coordinator listens to these guilds (smarter-dev-config).
GUILDS_ENV_VAR = "MODERATION_SHADOW_GUILD_IDS"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("status", "switch", "rollback"))
    parser.add_argument("--guild", required=True)
    parser.add_argument("--by", default="moderation-cutover-job")
    args = parser.parse_args(argv)
    async with get_db_session_context() as session:
        try:
            if args.command == "status":
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                result = await cutover.status(session, args.guild)
                await session.rollback()
            elif args.command == "switch":
                # Only a guild the bot listens to can be handed over; anything
                # else would turn the legacy actors off with nobody acting.
                listening = {
                    part.strip()
                    for part in os.getenv(GUILDS_ENV_VAR, "").split(",")
                    if part.strip()
                }
                if args.guild not in listening:
                    raise cutover.CutoverRefused(
                        f"guild {args.guild} is not in {GUILDS_ENV_VAR}; "
                        "nothing changed"
                    )
                result = await cutover.switch(session, args.guild, by=args.by)
            else:
                result = await cutover.rollback(session, args.guild, by=args.by)
        except cutover.CutoverRefused as refused:
            await session.rollback()
            logger.error("refused: %s", refused)
            return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    logger.info("%s done for guild %s", args.command, args.guild)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("moderation cutover failed")
        sys.exit(1)
