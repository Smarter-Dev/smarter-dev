"""Make the live Disboard bump tracker work in the fire's own channel (#242).

    check  --guild <id>                    read only: print what would change
    update --guild <id> --expect-sha <h>   patch it, in one transaction

The tracker's script was rendered with its bump channel as a constant,
``BUMP_CHANNEL``, and deletes and posts there. Its scope is now the moderation
policy's bump channels, so after a channel change on the Moderation page a
delete in the old channel is out of scope and fails the fire, after the
ledger write and before the crown rotates and the reminder is armed. The
patch drops the constant: every ``delete_message`` and the reminder go to the
fire's own channel (a timer fire's is the channel its bump came from).

The Disboard extension can't be re-rendered for this while the coordinator
owns the guild, so the row's script is edited in place, as exact textual
changes: the constant's line, the four ``delete_message(..., BUMP_CHANNEL)``
calls and the reminder's ``send_message``. Anything not exactly as expected
(a hand edit, a second tracker, no tracker) is refused with nothing changed.
``update`` also needs the script hash ``check`` printed, so a row edited in
between is refused. A row already patched prints "nothing to update".

Run in the cluster as a Job (``k8s/oneoff-patch-bump-tracker-channel.yaml``).
Exit status: 0 done, 2 refused (nothing changed), 1 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import sys

from sqlalchemy import select
from sqlalchemy import text

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.bump_channel_scope import bump_channel_ids
from smarter_dev.web.bump_channel_scope import is_bump_tracker
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.models import AdminHandler

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("patch_bump_tracker_channel")

_CONSTANT = re.compile(r'^BUMP_CHANNEL = "\d+"\n', re.MULTILINE)
_CONSTANT_NOTE = (
    "# The bump channel is the fire's own channel: the moderation policy's bump\n"
    "# channels decide where this runs (#242).\n"
)
_REMINDER = ", BUMP_CHANNEL, REMINDER_PING_ROLE\n"
_REMINDER_PATCHED = ", ping_role_id=REMINDER_PING_ROLE\n"
_DELETE = ", BUMP_CHANNEL)"
_DELETES = 4


class Refused(Exception):
    """Nothing was changed."""


def sha(script: str) -> str:
    return hashlib.sha256(script.encode()).hexdigest()[:12]


def is_patched(script: str) -> bool:
    return "BUMP_CHANNEL" not in script and _CONSTANT_NOTE in script


def patch_script(script: str) -> str:
    """The script with ``BUMP_CHANNEL`` gone; refuses an unexpected script."""
    if len(_CONSTANT.findall(script)) != 1:
        raise Refused("the script has no single BUMP_CHANNEL constant")
    if script.count(_REMINDER) != 1:
        raise Refused("the reminder's send_message is not as expected")
    if script.count(_DELETE) != _DELETES:
        raise Refused(
            f"expected {_DELETES} delete_message calls in BUMP_CHANNEL, "
            f"found {script.count(_DELETE)}"
        )
    patched = _CONSTANT.sub(_CONSTANT_NOTE, script)
    patched = patched.replace(_REMINDER, _REMINDER_PATCHED)
    patched = patched.replace(_DELETE, ")")
    if "BUMP_CHANNEL" in patched:
        raise Refused("BUMP_CHANNEL is still used elsewhere in the script")
    reason = moderation_refusal(patched) or lint_script(patched)
    if reason is not None:
        raise Refused(f"the patched script fails the save checks: {reason}")
    return patched


async def _tracker(session, guild_id: str) -> AdminHandler:
    rows = [
        row
        for row in (
            await session.execute(
                select(AdminHandler).where(AdminHandler.guild_id == guild_id)
            )
        ).scalars()
        if is_bump_tracker(row)
    ]
    if len(rows) != 1:
        raise Refused(
            f"guild {guild_id} has {len(rows)} bump trackers, not 1; nothing changed"
        )
    return rows[0]


async def check(session, guild_id: str) -> list[str]:
    """What ``update`` would change. Changes nothing."""
    row = await _tracker(session, guild_id)
    channels = await bump_channel_ids(session, guild_id)
    head = (
        f"admin_handlers id={row.id} name={row.name} enabled={row.enabled} "
        f"script sha256 {sha(row.script)}; policy bump channels {channels}"
    )
    if is_patched(row.script):
        return [head, "already patched; nothing to update"]
    patched = patch_script(row.script)
    return [head, f"would patch: script sha256 {sha(row.script)} -> {sha(patched)}"]


async def update(session, guild_id: str, expect_sha: str) -> list[str]:
    """Patch the tracker's script and commit; what was changed."""
    row = await _tracker(session, guild_id)
    if is_patched(row.script):
        return [f"admin_handlers id={row.id} already patched; nothing to update"]
    before = sha(row.script)
    if before != expect_sha:
        raise Refused(
            f"script sha256 is {before}, not the {expect_sha} given; "
            "run check again; nothing changed"
        )
    row.script = patch_script(row.script)
    await session.commit()
    return [f"admin_handlers id={row.id} script sha256 {before} -> {sha(row.script)}"]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "update"))
    parser.add_argument("--guild", required=True)
    parser.add_argument("--expect-sha")
    args = parser.parse_args(argv)
    if args.command == "update" and not args.expect_sha:
        parser.error("update needs --expect-sha, the script sha256 check printed")
    async with get_db_session_context() as session:
        try:
            if args.command == "check":
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                lines = await check(session, args.guild)
                await session.rollback()
            else:
                lines = await update(session, args.guild, args.expect_sha)
        except Refused as refused:
            await session.rollback()
            logger.error("refused: %s", refused)
            return 2
    for line in lines:
        print(f"guild {args.guild}: {line}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("patching the bump tracker failed")
        sys.exit(1)
