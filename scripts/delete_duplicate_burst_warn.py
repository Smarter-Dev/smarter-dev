"""Delete the second warning one scam burst gave (#376).

    check    read only: print the row that would be deleted
    delete   delete it, in one transaction

On Oct 25 2026 at 14:07Z member 1328112859804336178 posted two different scam
posts, each in two channels. The coordinator judged the two posts apart and
warned the member once per post: two incidents (6ebbd00d and d37d0554), two
warnings. One burst owes one warning, so this deletes the later
``moderation_actions`` row (incident d37d0554). Its gateway operation
(9aa5fc55) stays as the record of what was done; nothing else is touched.

Refused, with nothing changed, unless the row exists and still is exactly
that warning (guild, member, ``warn``, incident, operation), and the first
warning (incident 6ebbd00d) is still on record, so the member keeps one. A
second ``delete`` finds nothing and exits 0.

Run in the cluster as a Job (``k8s/oneoff-delete-duplicate-burst-warn.yaml``).
Exit status: 0 done, 2 refused (nothing changed), 1 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import ModerationAction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("delete_duplicate_burst_warn")

GUILD = "644299523686006834"
MEMBER = "1328112859804336178"
#: The second warning, and what it must still be.
DUPLICATE = UUID("5a99158f-d032-45b4-8fd9-fac1e464c117")
DUPLICATE_INCIDENT = UUID("d37d0554-fbe7-4426-9788-61caec587193")
DUPLICATE_OPERATION = UUID("9aa5fc55-03ba-49af-bda5-0326bed6888a")
#: The first warning, which stays.
KEPT = UUID("31484975-fc29-417f-ab1e-e2fce961b626")
KEPT_INCIDENT = UUID("6ebbd00d-c582-4f79-a10e-831466066f93")


class Refused(Exception):
    """Nothing was changed."""


def describe(row: ModerationAction) -> str:
    """Every column of the row, as JSON (a warning row holds no secrets)."""
    values = {
        column.key: getattr(row, column.key)
        for column in inspect(ModerationAction).mapper.column_attrs
    }
    return json.dumps(values, default=str, sort_keys=True)


async def _rows(session, *, lock: bool):
    """The duplicate (None when already deleted) and the kept warning, after
    every check passed. ``lock`` takes row locks for ``delete``."""

    def read(action_id: UUID):
        statement = select(ModerationAction).where(ModerationAction.id == action_id)
        return session.execute(statement.with_for_update() if lock else statement)

    kept = (await read(KEPT)).scalar_one_or_none()
    if (
        kept is None
        or kept.guild_id != GUILD
        or kept.target_user_id != MEMBER
        or kept.action_type != "warn"
        or kept.incident_id != KEPT_INCIDENT
    ):
        raise Refused(
            f"the warning that stays ({KEPT}) is not the expected row; nothing changed"
        )
    row = (await read(DUPLICATE)).scalar_one_or_none()
    if row is None:
        return None, kept
    mismatches = [
        name
        for name, actual, expected in (
            ("guild_id", row.guild_id, GUILD),
            ("target_user_id", row.target_user_id, MEMBER),
            ("action_type", row.action_type, "warn"),
            ("incident_id", row.incident_id, DUPLICATE_INCIDENT),
            ("operation_id", row.operation_id, DUPLICATE_OPERATION),
        )
        if actual != expected
    ]
    if mismatches:
        raise Refused(
            f"row {DUPLICATE} does not match on {', '.join(mismatches)}; "
            "nothing changed"
        )
    return row, kept


async def check(session) -> list[str]:
    """What ``delete`` would remove. Changes nothing."""
    row, kept = await _rows(session, lock=False)
    lines = [f"keeps moderation_actions {describe(kept)}"]
    if row is not None:
        lines.append(f"would delete moderation_actions {describe(row)}")
    return lines


async def delete(session) -> list[str]:
    """Delete the duplicate and commit; what was deleted."""
    row, kept = await _rows(session, lock=True)
    lines = [f"keeps moderation_actions {describe(kept)}"]
    if row is None:
        await session.rollback()
        return lines
    lines.append(f"deleting moderation_actions {describe(row)}")
    # Logged before the delete: the row is on record even if it fails.
    for line in lines:
        print(line, flush=True)
    await session.delete(row)
    await session.commit()
    return [f"deleted moderation_actions id={DUPLICATE}"]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "delete"))
    args = parser.parse_args(argv)
    async with get_db_session_context() as session:
        try:
            if args.command == "check":
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                lines = await check(session)
                await session.rollback()
            else:
                lines = await delete(session)
        except Refused as refused:
            await session.rollback()
            logger.error("refused: %s", refused)
            return 2
    for line in lines:
        print(line)
    if args.command == "delete" and not any(
        line.startswith("deleted") for line in lines
    ):
        print(f"moderation_actions {DUPLICATE}: nothing to delete")
    logger.info("%s done", args.command)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("deleting the duplicate burst warning failed")
        sys.exit(1)
