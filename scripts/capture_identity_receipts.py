"""Restricted operator snapshot; contains UUIDs, so never print its contents.

Run from an approved trusted environment with a restricted durable destination.
This does not pause admission or establish transition safety by itself.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from sqlalchemy import text


async def capture_rows(connection):
    # Caller must use REPEATABLE READ. This first query establishes the MVCC
    # boundary BEFORE reading the rows, including uncommitted purge updates.
    boundary = (
        await connection.execute(
            text("SELECT pg_current_snapshot()::text, clock_timestamp()::text")
        )
    ).one()
    rows = (
        (
            await connection.execute(
                text(
                    "SELECT id, user_id, status, json_array_length(subscription_ids) > 0 AS retry "
                    "FROM skrift.account_deletion_requests ORDER BY id"
                )
            )
        )
        .mappings()
        .all()
    )
    return {
        "version": 1,
        "snapshot": boundary[0],
        "captured_at": boundary[1],
        "receipts": [
            {
                "id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "recoverable": row["status"] != "complete" or row["retry"],
            }
            for row in rows
        ],
    }


def persist_snapshot(path: Path, snapshot):
    """Exclusive creation, fsync and read-back; no success on partial write."""
    payload = json.dumps(snapshot, sort_keys=True).encode()
    # Do not overwrite either a completed or failed attempt; operator assesses
    # and retries at a new path. A failed file is never capture-verified.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    if path.read_bytes() != payload:
        raise RuntimeError("Snapshot verification failed")
    return hashlib.sha256(payload).hexdigest()


def reconcile_captures(before, after):
    """Return only verified original UUIDs, or fail on a transition gap.

    Completed-at-initial-boundary anonymization is counted as historical. Any new
    completed receipt absent from initial recoverable rows is a TRANSITION failure.
    """
    old = {row["id"]: row for row in before["receipts"]}
    new = {row["id"]: row for row in after["receipts"]}
    if set(old) - set(new):
        raise RuntimeError("Receipt disappeared during transition")
    uuids = {row["user_id"] for row in old.values() if row["recoverable"]}
    for rid, row in new.items():
        if row["recoverable"]:
            if rid in old and (
                not old[rid]["recoverable"] or old[rid]["user_id"] != row["user_id"]
            ):
                raise RuntimeError("Receipt identity changed during transition")
            uuids.add(row["user_id"])
        elif rid not in old:
            raise RuntimeError("New receipt anonymized during transition")
    return sorted(uuids)


async def capture(path):
    from smarter_dev.shared.database import get_engine

    async with get_engine().connect() as connection:
        connection = await connection.execution_options(
            isolation_level="REPEATABLE READ"
        )
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout = '30s'"))
            snapshot = await capture_rows(connection)
            # Do not stop the worker until this returns and an independent
            # restricted copy/hash verification is recorded by the operator.
            return persist_snapshot(path, snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        checksum = asyncio.run(capture(args.destination))
    except Exception:
        print(
            "Receipt capture failed; keep provisioning disabled and do not terminate writers.",
            file=sys.stderr,
        )
        return 1
    print(f"Receipt capture durably verified; SHA256={checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
