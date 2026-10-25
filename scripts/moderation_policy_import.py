"""Write revision 1 of a guild's moderation policy from the config it runs on.

    status  --guild <id>               read only: the guild's policy revision,
                                       or none, and who has loaded which
    dry-run --guild <id>               read only: the document the import
                                       would write, where each part came from,
                                       and anything to check first
    import  --guild <id> --by <who>    write it as revision 1 (refused if the
                                       guild has a policy, or the coordinator
                                       does not own it)

Run in the cluster as a Job (``k8s/oneoff-moderation-policy-import.yaml``).
The mapping is in ``smarter_dev/web/moderation_policy_import.py``. Exit
status: 0 done, 2 refused (nothing changed), 1 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy import text

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web import moderation_policy_import as policy_import
from smarter_dev.web import moderation_policy_store as store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("moderation_policy_import")


async def _status(session, guild_id: str) -> dict:
    stored = await store.read_policy(session, guild_id)
    return {
        "guild_id": guild_id,
        "revision": stored.revision if stored else None,
        "updated_by": stored.updated_by if stored else None,
        "updated_at": stored.updated_at.isoformat()
        if stored and stored.updated_at
        else None,
        "loads": [
            {
                "process": row.process,
                "revision": row.revision,
                "loaded_at": row.loaded_at.isoformat(),
                "seen_at": row.seen_at.isoformat(),
            }
            for row in await store.loads(session, guild_id)
        ],
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("status", "dry-run", "import"))
    parser.add_argument("--guild", required=True)
    parser.add_argument("--by", default="moderation-policy-import-job")
    args = parser.parse_args(argv)
    async with get_db_session_context() as session:
        try:
            if args.command in ("status", "dry-run"):
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                if args.command == "status":
                    result = await _status(session, args.guild)
                else:
                    result = (await policy_import.plan(session, args.guild)).as_json()
                await session.rollback()
            else:
                plan = await policy_import.run(session, args.guild, by=args.by)
                result = {**plan.as_json(), **await _status(session, args.guild)}
        except (policy_import.ImportRefused, store.StaleRevision) as refused:
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
        logger.exception("moderation policy import failed")
        sys.exit(1)
