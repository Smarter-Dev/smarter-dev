#!/usr/bin/env python
"""Seed the /resources/* tables from the legacy Python data modules.

Idempotent. Safe to re-run after the data files have been edited; existing
rows are updated and missing rows are inserted. Usage:

    uv run python scripts/seed_resources.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
from smarter_dev.web._resources_seed import seed_all


async def main() -> None:
    settings = get_settings()
    url = convert_postgres_url_for_asyncpg(settings.effective_database_url)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(__import__("sqlalchemy").text("SET search_path TO skrift, public"))
            await conn.run_sync(seed_all)
    finally:
        await engine.dispose()
    print("seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
