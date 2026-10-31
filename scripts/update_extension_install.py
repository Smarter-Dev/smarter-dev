"""Update one guild's extension install to the catalog's current version (#242).

    check  --guild <id> --slug <slug>    read only: print what would change
    update --guild <id> --slug <slug>    update it, in one transaction

The same update the admin page's "Update" button does
(``extension_installs.update_extension``): the install's handler rows are
re-rendered from the current catalog template with the install's saved
config, and a config field the new version dropped is discarded. Written for
Rejoin Alert v1 → v2 (#242), which reports through the moderation system
instead of posting in its own mod-log channel.

Refused, with nothing changed, when the guild has no such install or the slug
is not in the catalog. ``check`` prints each owned row's current script hash,
so a hand edit the update would overwrite can be spotted first. An install
already at the catalog version prints "nothing to update" and exits 0.

Run in the cluster as a Job (``k8s/oneoff-update-extension-install.yaml``).
Exit status: 0 done, 2 refused (nothing changed), 1 failed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys

from sqlalchemy import select
from sqlalchemy import text

from smarter_dev.extensions.registry import ExtensionRegistryError
from smarter_dev.extensions.registry import get_registry
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.extension_installs import update_extension
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ExtensionInstall

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("update_extension_install")


class Refused(Exception):
    """Nothing was changed."""


def _sha(text_: str) -> str:
    return hashlib.sha256(text_.encode()).hexdigest()[:12]


async def _plan(
    session, guild_id: str, slug: str
) -> tuple[ExtensionInstall, list[str]]:
    """The install and what ``update`` would change; refuses as documented."""
    install = (
        await session.execute(
            select(ExtensionInstall).where(
                ExtensionInstall.guild_id == guild_id,
                ExtensionInstall.extension_slug == slug,
            )
        )
    ).scalar_one_or_none()
    if install is None:
        raise Refused(f"guild {guild_id} has no {slug} install; nothing changed")
    try:
        loaded = get_registry().get(slug)
    except ExtensionRegistryError:
        raise Refused(f"{slug} is not in the catalog; nothing changed") from None
    target = loaded.manifest.version
    if install.installed_version >= target:
        return install, []
    rows = list(
        (
            await session.execute(
                select(AdminHandler)
                .where(AdminHandler.extension_install_id == install.id)
                .order_by(AdminHandler.name)
            )
        ).scalars()
    )
    lines = [
        f"install {slug} v{install.installed_version} -> v{target}, "
        f"config {sorted(install.config or {})} enabled={install.enabled}"
    ]
    for row in rows:
        lines.append(
            f"admin_handlers id={row.id} name={row.name} script sha256 "
            f"{_sha(row.script)} (re-rendered from v{target})"
        )
    return install, lines


async def check(session, guild_id: str, slug: str) -> list[str]:
    """What ``update`` would change. Changes nothing."""
    _install, lines = await _plan(session, guild_id, slug)
    return lines


async def update(session, guild_id: str, slug: str) -> list[str]:
    """Update the install and commit; what was changed."""
    install, lines = await _plan(session, guild_id, slug)
    if not lines:
        return []
    updated = await update_extension(session, guild_id=guild_id, slug=slug)
    rows = list(
        (
            await session.execute(
                select(AdminHandler)
                .where(AdminHandler.extension_install_id == install.id)
                .order_by(AdminHandler.name)
            )
        ).scalars()
    )
    return [
        f"install {slug} now v{updated.installed_version}, "
        f"config {sorted(updated.config or {})}"
    ] + [
        f"admin_handlers id={row.id} name={row.name} script sha256 {_sha(row.script)}"
        for row in rows
    ]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "update"))
    parser.add_argument("--guild", required=True)
    parser.add_argument("--slug", required=True)
    args = parser.parse_args(argv)
    async with get_db_session_context() as session:
        try:
            if args.command == "check":
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                lines = await check(session, args.guild, args.slug)
                await session.rollback()
            else:
                lines = await update(session, args.guild, args.slug)
        except Refused as refused:
            await session.rollback()
            logger.error("refused: %s", refused)
            return 2
    verb = "would update" if args.command == "check" else "updated"
    if not lines:
        print(
            f"guild {args.guild}: {args.slug} is at the catalog version; nothing to update"
        )
    for line in lines:
        print(f"guild {args.guild}: {verb} {line}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("updating the extension install failed")
        sys.exit(1)
