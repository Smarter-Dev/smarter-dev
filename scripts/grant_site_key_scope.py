"""Add one scope to the site service key in place, without re-minting it.

``python -m scripts.grant_site_key_scope site-code`` finds the single active
site service key, checks its owner already holds the permission (the deploy
syncs the ``site-service`` role), and adds the scope to the key's own list
with ``update_api_key``. The key itself, and the Worker's ``SKRIFT_SITE_KEY``,
stay the same. Prints the key's prefix and its scopes, never the key. Running
it again changes nothing. See docs/site-code.md.
"""

from __future__ import annotations

import asyncio
import sys

from skrift.auth.services import get_user_permissions
from skrift.db.models.api_key import APIKey
from skrift.db.services.api_key_service import update_api_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def grant(session: AsyncSession, scope: str) -> str:
    """Add ``scope`` to the active site key; what was done, for the log."""
    keys = (
        await session.scalars(
            select(APIKey).where(
                APIKey.principal_type == "service",
                APIKey.service_name == "site",
                APIKey.is_active.is_(True),
            )
        )
    ).all()
    if len(keys) != 1:
        raise RuntimeError(f"expected one active site key, found {len(keys)}")
    key = keys[0]
    owner = await get_user_permissions(session, str(key.user_id))
    if scope not in owner.permissions:
        raise RuntimeError(
            f"the site key's owner lacks {scope}; deploy first so roles sync"
        )
    scopes = set(key.scoped_permission_list)
    if scope in scopes:
        return f"{key.key_prefix}: {scope} already granted"
    await update_api_key(session, key, scoped_permissions=sorted(scopes | {scope}))
    return f"{key.key_prefix}: {sorted(scopes | {scope})}"


async def _main(scope: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
    from smarter_dev.shared.database import create_async_engine

    url = convert_postgres_url_for_asyncpg(get_settings().effective_database_url)
    engine = create_async_engine(url).execution_options(
        schema_translate_map={None: "skrift"}
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            print(await grant(session, scope), flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.grant_site_key_scope <scope>")
    asyncio.run(_main(sys.argv[1]))
