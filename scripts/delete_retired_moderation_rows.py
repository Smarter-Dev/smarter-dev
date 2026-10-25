"""Delete the disabled moderation rows the #240 cutover left behind.

    check  --guild <id>    read only: print what would be deleted
    delete --guild <id>    delete it, in one transaction

The cutover (``scripts/moderation_cutover.py``) disabled, and did not delete,
the guild's ``scam-banner`` and ``edit-ping-catch`` admin handlers and its
``edit-ping-catch`` extension install. The code those rows ran was removed
after the cutover, so this deletes them: the handler rows first, then the
install, as ``extension_installs.uninstall_extension`` does. ``handler_runs``
history is kept (there are no foreign keys; old runs simply name a handler
that no longer exists).

Refused, with nothing changed, unless the guild's ``moderation_ownership`` row
names the coordinator, when any of those rows is still enabled, when the
edit-ping-catch handler belongs to a different install than the guild's
edit-ping-catch install, or when either handler is scoped to channels (the
coordinator reads those scopes from the rows, so deleting a scoped row would
widen its rule to every channel), or when the install owns any other handler
row. A second ``delete`` finds nothing and exits 0.

Run in the cluster as a Job (``k8s/oneoff-delete-retired-moderation.yaml``).
Exit status: 0 done, 2 refused (nothing changed), 1 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy import text

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ExtensionInstall
from smarter_dev.web.models import ModerationOwnership

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("delete_retired_moderation_rows")

SCAM_BANNER_HANDLER = "scam-banner"
EDIT_PING_HANDLER = "edit-ping-catch"
EDIT_PING_SLUG = "edit-ping-catch"
RETIRED_HANDLERS = (SCAM_BANNER_HANDLER, EDIT_PING_HANDLER)
COORDINATOR = "coordinator"


class Refused(Exception):
    """Nothing was changed."""


async def _retired_rows(
    session, guild_id: str, *, lock: bool
) -> tuple[list[AdminHandler], ExtensionInstall | None]:
    """The guild's retired rows, after every refusal check passed.

    ``lock`` takes row locks for ``delete``; ``check`` runs read only, where
    PostgreSQL refuses ``SELECT ... FOR UPDATE``.
    """

    def locked(statement):
        return statement.with_for_update() if lock else statement

    ownership = (
        await session.execute(
            locked(
                select(ModerationOwnership).where(
                    ModerationOwnership.guild_id == guild_id
                )
            )
        )
    ).scalar_one_or_none()
    if ownership is None or ownership.owner != COORDINATOR:
        owner = ownership.owner if ownership else "no ownership row"
        raise Refused(
            f"guild {guild_id} is not owned by the coordinator ({owner}); "
            "nothing changed"
        )
    handlers = list(
        (
            await session.execute(
                locked(
                    select(AdminHandler)
                    .where(
                        AdminHandler.guild_id == guild_id,
                        AdminHandler.name.in_(RETIRED_HANDLERS),
                    )
                    .order_by(AdminHandler.name)
                )
            )
        ).scalars()
    )
    install = (
        await session.execute(
            locked(
                select(ExtensionInstall).where(
                    ExtensionInstall.guild_id == guild_id,
                    ExtensionInstall.extension_slug == EDIT_PING_SLUG,
                )
            )
        )
    ).scalar_one_or_none()

    enabled = [f"handler {row.name}" for row in handlers if row.enabled]
    if install is not None and install.enabled:
        enabled.append(f"install {install.extension_slug}")
    if enabled:
        raise Refused(
            f"still enabled in guild {guild_id}: {', '.join(enabled)}; nothing changed"
        )
    for row in handlers:
        if row.channel_ids:
            raise Refused(
                f"handler {row.name} is scoped to channels {list(row.channel_ids)}; "
                "the coordinator reads that scope from the row, so deleting it "
                "would widen the rule to every channel; nothing changed"
            )
    edit = next((row for row in handlers if row.name == EDIT_PING_HANDLER), None)
    if (
        edit is not None
        and install is not None
        and edit.extension_install_id != install.id
    ):
        raise Refused(
            f"handler {EDIT_PING_HANDLER} belongs to install "
            f"{edit.extension_install_id}, not the guild's {EDIT_PING_SLUG} "
            f"install {install.id}; nothing changed"
        )
    if install is not None:
        others = (
            (
                await session.execute(
                    select(AdminHandler.name).where(
                        AdminHandler.extension_install_id == install.id,
                        AdminHandler.name.not_in(RETIRED_HANDLERS),
                    )
                )
            )
            .scalars()
            .all()
        )
        if others:
            raise Refused(
                f"the {EDIT_PING_SLUG} install also owns {sorted(others)}; "
                "nothing changed"
            )
    return handlers, install


def _describe(
    handlers: list[AdminHandler], install: ExtensionInstall | None
) -> list[str]:
    lines = [
        f"admin_handlers id={row.id} name={row.name} enabled={row.enabled}"
        for row in handlers
    ]
    if install is not None:
        lines.append(
            f"extension_installs id={install.id} name={install.extension_slug} "
            f"enabled={install.enabled}"
        )
    return lines


async def check(session, guild_id: str) -> list[str]:
    """What ``delete`` would remove. Changes nothing."""
    handlers, install = await _retired_rows(session, guild_id, lock=False)
    return _describe(handlers, install)


async def delete(session, guild_id: str) -> list[str]:
    """Delete the retired rows and commit; what was deleted."""
    handlers, install = await _retired_rows(session, guild_id, lock=True)
    deleted = _describe(handlers, install)
    for row in handlers:
        await session.delete(row)
    # Handler rows first, then the install, as uninstall_extension does.
    await session.flush()
    if install is not None:
        await session.delete(install)
    await session.commit()
    return deleted


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "delete"))
    parser.add_argument("--guild", required=True)
    args = parser.parse_args(argv)
    async with get_db_session_context() as session:
        try:
            if args.command == "check":
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                lines = await check(session, args.guild)
                await session.rollback()
            else:
                lines = await delete(session, args.guild)
        except Refused as refused:
            await session.rollback()
            logger.error("refused: %s", refused)
            return 2
    verb = "would delete" if args.command == "check" else "deleted"
    if not lines:
        print(f"guild {args.guild}: nothing to delete")
    for line in lines:
        print(f"guild {args.guild}: {verb} {line}")
    logger.info("%s done for guild %s", args.command, args.guild)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("deleting the retired moderation rows failed")
        sys.exit(1)
