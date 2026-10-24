"""A small curated catalog in a LOCAL PostgreSQL, for the corpus tests.

Full-text search and pg_trgm need PostgreSQL: opt in with
RESOURCES_TEST_POSTGRES_URL pointing at an ephemeral local database with the
pg_trgm extension. The resource tables are created and dropped there.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.web.models import ResourceCategory
from smarter_dev.web.models import ResourceDirectory
from smarter_dev.web.models import ResourceDirectorySpine
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.models import ResourceTool
from smarter_dev.web.models import ResourceToolSource

URL = os.getenv("RESOURCES_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(
    not URL, reason="ephemeral local PostgreSQL not configured"
)
TABLES = [
    ResourceDirectory.__table__,
    ResourceCategory.__table__,
    ResourceTool.__table__,
    ResourceSource.__table__,
    ResourceDirectorySpine.__table__,
    ResourceToolSource.__table__,
]

WARM = "https://example.test/postgres-skip-locked"
LONG = "https://example.test/long-read"
COLD = "https://example.test/feature-flags"
STALE = "https://example.test/incident-runbooks"
TOOL = "https://example.test/tools/pgbouncer"
# A stored body longer than a fresh read keeps (scripts/warm_jina_cache.py
# and older reads stored up to 12,000 characters).
LONG_BODY = "Long read\n\n" + "x" * 11_989


async def seed(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for table in reversed(TABLES):
            await conn.run_sync(table.drop, checkfirst=True)
        for table in TABLES:
            await conn.run_sync(table.create)
        directory, category, tool = uuid4(), uuid4(), uuid4()
        await conn.execute(
            text(
                "INSERT INTO resource_directories VALUES "
                "(:id, 'production-operations', 'Production operations', 'ops', 1)"
            ),
            {"id": directory},
        )
        await conn.execute(
            text(
                "INSERT INTO resource_categories "
                "(id, directory_id, slug, name, sort_order) "
                "VALUES (:id, :d, 'databases', 'Databases', 1)"
            ),
            {"id": category, "d": directory},
        )
        await conn.execute(
            text(
                "INSERT INTO resource_tools VALUES (:id, :c, 'pgbouncer', "
                "'PgBouncer', :url, 'ops:tool:pgbouncer', "
                "'Lightweight connection pooler for Postgres.', 1)"
            ),
            {"id": tool, "c": category, "url": TOOL},
        )
        now = datetime.now(UTC)
        rows = [
            (
                WARM,
                "Postgres SKIP LOCKED queues",
                "Build a job queue on Postgres with SKIP LOCKED.",
                "Postgres SKIP LOCKED queues\n\nThe full article text.",
                now,
            ),
            (LONG, "A long read on queues", "Everything about queues.", LONG_BODY, now),
            (
                COLD,
                "Feature flags in practice",
                "Ship dark, release safely.",
                None,
                None,
            ),
            (
                STALE,
                "Incident response runbooks",
                "Writing runbooks people use at 3am.",
                "old body",
                now - timedelta(days=31),
            ),
        ]
        for index, (url, title, blurb, body, fetched) in enumerate(rows):
            source = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO resource_sources (id, track_key, title, url, "
                    "byline, blurb, learning_type, first_indexed_at, "
                    "jina_content, jina_fetched_at) VALUES (:id, :k, :t, :u, "
                    "'Example', :b, 'Article', :f, :body, :at)"
                ),
                {
                    "id": source,
                    "k": f"ops:spine:{index}",
                    "t": title,
                    "u": url,
                    "b": blurb,
                    "f": (now - timedelta(days=index)).date(),
                    "body": body,
                    "at": fetched,
                },
            )
            await conn.execute(
                text("INSERT INTO resource_directory_spine VALUES (:d, :s, :o)"),
                {"d": directory, "s": source, "o": index},
            )
            if url == WARM:
                await conn.execute(
                    text("INSERT INTO resource_tool_sources VALUES (:t, :s, 1)"),
                    {"t": tool, "s": source},
                )


async def drop(engine) -> None:
    async with engine.begin() as conn:
        for table in reversed(TABLES):
            await conn.run_sync(table.drop, checkfirst=True)


async def stored(engine, url):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT jina_content, jina_fetched_at FROM resource_sources "
                    "WHERE url = :u"
                ),
                {"u": url},
            )
        ).first()


@pytest.fixture
async def corpus(monkeypatch):
    """The catalog seeded fresh, and every DB lookup (old code and new) on
    it: both read the URL from get_settings."""
    from smarter_dev.shared import config

    assert make_url(URL).host in {"127.0.0.1", "localhost"}
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(effective_database_url=URL),
    )
    engine = create_async_engine(URL, poolclass=NullPool)
    await seed(engine)
    yield engine
    await drop(engine)
    await engine.dispose()
