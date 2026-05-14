#!/usr/bin/env python
"""Precrawl every curated resource_source URL through Jina Reader.

Walks ``resource_sources``, calls Jina Reader for any row whose
``jina_content`` is empty or older than 30 days, and writes the cleaned body
back into the row. After this runs once, the agent's ``read_source`` tool
serves curated URLs from Postgres with zero Jina latency — runs that
previously took ~45 s of model + IO collapse to model time only.

Usage:

    uv run python scripts/warm_jina_cache.py                    # missing/stale only
    uv run python scripts/warm_jina_cache.py --force            # refresh everything
    uv run python scripts/warm_jina_cache.py --concurrency 8    # tune in-flight fetches

Environment:
    JINA_API_KEY    optional; jina_read works anonymously but with tighter rate limits.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.scan.tools import jina_read

logger = logging.getLogger("warm_jina_cache")

_MAX_CHARS = 10_000
_DEFAULT_TTL_DAYS = 30


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    session_factory,
    src: ResourceSource,
) -> tuple[str, str]:
    async with sem:
        result = await jina_read(client, src.url)

    if "error" in result:
        return (src.url, f"error: {result['error']}")

    title = result.get("title", "") or ""
    content = result.get("content", "") or ""
    body = f"{title}\n\n{content}".strip()[:_MAX_CHARS]
    if not body:
        return (src.url, "empty body")

    async with session_factory() as s:
        await s.execute(sql_text("SET search_path TO skrift, public"))
        await s.execute(
            sql_text(
                "UPDATE resource_sources "
                "SET jina_content = :body, jina_fetched_at = :now "
                "WHERE id = :id"
            ),
            {
                "body": body,
                "now": datetime.now(timezone.utc),
                "id": src.id,
            },
        )
        await s.commit()
    return (src.url, f"ok ({len(body)} chars)")


def _is_stale(src: ResourceSource, ttl: timedelta) -> bool:
    if not src.jina_content or not src.jina_fetched_at:
        return True
    return datetime.now(timezone.utc) - src.jina_fetched_at > ttl


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even fresh rows (otherwise only missing/stale ones).",
    )
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=_DEFAULT_TTL_DAYS,
        help=f"Staleness threshold in days (default: {_DEFAULT_TTL_DAYS}).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max concurrent Jina fetches (default: 4).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N fetches (for testing).",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    ttl = timedelta(days=args.ttl_days)
    try:
        async with Session() as s:
            await s.execute(sql_text("SET search_path TO skrift, public"))
            all_sources = (await s.execute(select(ResourceSource))).scalars().all()

        targets = [
            src for src in all_sources if args.force or _is_stale(src, ttl)
        ]
        if args.limit is not None:
            targets = targets[: args.limit]

        logger.info(
            "found %d sources total, %d to warm (force=%s, ttl=%dd)",
            len(all_sources),
            len(targets),
            args.force,
            args.ttl_days,
        )
        if not targets:
            return 0

        sem = asyncio.Semaphore(max(1, args.concurrency))
        async with httpx.AsyncClient() as client:
            tasks = [
                _fetch_one(client, sem, Session, src) for src in targets
            ]
            completed = 0
            for coro in asyncio.as_completed(tasks):
                url, status = await coro
                completed += 1
                logger.info("[%d/%d] %s — %s", completed, len(targets), status, url)
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
