"""Searching and reading the curated resource corpus.

One implementation for every caller: the Resources researcher's
``search_resources`` / ``read_source`` tools (and the gap-filler's
``read_url``) and the site service API that the site's chat calls
(``api_native/site_resources.py``). Both go through :func:`search_corpus`
and :func:`read_corpus`, so their ranking and their cache can't drift apart.

Search ranks titles and blurbs: an English full-text match scores 0.4, and
trigram similarity lets near-misses through ("postgres queue" finds
"Postgres SKIP LOCKED queues"). Article text is not searched.

Reads come from a tiered cache:

1. ``resource_sources.jina_content``: the durable Postgres cache, filled by
   ``scripts/warm_jina_cache.py`` and by reads, fresh for 30 days.
2. An in-process cache (60 minutes) for URLs that aren't curated rows.
3. Live Jina Reader on a miss, written back to whichever layer applies.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import httpx

from smarter_dev.web.research_tools import jina_read

READ_MAX_CHARS = 10_000
READ_TTL_SECONDS = 60 * 60  # in-process cache, for non-curated URLs
READ_DB_TTL_DAYS = 30  # how long a stored Jina body is considered fresh
SEARCH_MAX_LIMIT = 20

_READ_CACHE: dict[str, tuple[float, str]] = {}


def _engine():
    """A short-lived engine on the main DB, outside any request transaction."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import convert_postgres_url_for_asyncpg

    return create_async_engine(
        convert_postgres_url_for_asyncpg(get_settings().effective_database_url),
        poolclass=NullPool,
    )


_SOURCES_SQL = """
SELECT s.title, s.url, s.byline, s.blurb, s.learning_type,
       d.slug AS directory, COALESCE(c.slug, '') AS category,
       GREATEST(
         similarity(s.title, :q),
         similarity(coalesce(s.blurb, ''), :q),
         CASE WHEN to_tsvector('english',
                coalesce(s.title,'') || ' ' || coalesce(s.blurb,'')
              ) @@ plainto_tsquery('english', :q) THEN 0.4 ELSE 0 END
       ) AS score
FROM resource_sources s
LEFT JOIN resource_directory_spine dsp ON dsp.source_id = s.id
LEFT JOIN resource_directories d ON d.id = dsp.directory_id
LEFT JOIN resource_tool_sources ts ON ts.source_id = s.id
LEFT JOIN resource_tools t ON t.id = ts.tool_id
LEFT JOIN resource_categories c ON c.id = t.category_id
WHERE
  to_tsvector('english',
    coalesce(s.title,'') || ' ' || coalesce(s.blurb,'')
  ) @@ plainto_tsquery('english', :q)
  OR similarity(s.title, :q) > 0.15
  OR similarity(coalesce(s.blurb, ''), :q) > 0.15
ORDER BY score DESC NULLS LAST, s.first_indexed_at DESC
LIMIT :limit
"""

_TOOLS_SQL = """
SELECT t.name AS title, t.url, '' AS byline, t.blurb,
       'Tool' AS learning_type, d.slug AS directory, c.slug AS category,
       GREATEST(
         similarity(t.name, :q),
         similarity(coalesce(t.blurb, ''), :q),
         CASE WHEN to_tsvector('english',
                coalesce(t.name,'') || ' ' || coalesce(t.blurb,'')
              ) @@ plainto_tsquery('english', :q) THEN 0.4 ELSE 0 END
       ) AS score
FROM resource_tools t
JOIN resource_categories c ON c.id = t.category_id
JOIN resource_directories d ON d.id = c.directory_id
WHERE
  to_tsvector('english',
    coalesce(t.name,'') || ' ' || coalesce(t.blurb,'')
  ) @@ plainto_tsquery('english', :q)
  OR similarity(t.name, :q) > 0.15
  OR similarity(coalesce(t.blurb, ''), :q) > 0.15
ORDER BY score DESC NULLS LAST, t.name
LIMIT :tool_limit
"""


async def search_corpus(query: str, limit: int = 8) -> list[dict]:
    """Ranked catalog hits for ``query``: sources first, then tools.

    Each hit is ``{kind, title, url, byline, blurb, learning_type,
    directory, category}``; ``kind`` is "source" for curated articles and
    "tool" for indexed tools. A URL appears once.
    """
    from sqlalchemy import text

    if not query or not query.strip():
        return []
    limit = max(1, min(limit, SEARCH_MAX_LIMIT))

    engine = _engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            src_rows = (
                (await conn.execute(text(_SOURCES_SQL), {"q": query, "limit": limit}))
                .mappings()
                .all()
            )
            tool_rows = (
                (
                    await conn.execute(
                        text(_TOOLS_SQL),
                        {"q": query, "tool_limit": max(2, limit // 2)},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    hits: list[dict] = []
    seen: set[str] = set()
    for kind, rows in (("source", src_rows), ("tool", tool_rows)):
        for row in rows:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            hits.append(
                {
                    "kind": kind,
                    "title": row["title"],
                    "url": row["url"],
                    "byline": (row["byline"] or "") if kind == "source" else "",
                    "blurb": row["blurb"] or "",
                    "learning_type": row["learning_type"],
                    "directory": row["directory"],
                    "category": row["category"] or "",
                }
            )
    return hits[:limit]


@dataclass(frozen=True)
class CorpusRead:
    """A read: the body, or ``error`` saying why there is none.

    ``curated`` says whether the URL is a ``resource_sources`` row, and
    ``cached`` whether the body came from a cache rather than Jina."""

    body: str | None
    error: str | None
    curated: bool
    cached: bool


async def _stored(url: str) -> tuple[bool, str | None]:
    """Whether ``url`` is curated, and its stored body if that is fresh."""
    from sqlalchemy import text

    engine = _engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            row = (
                await conn.execute(
                    text(
                        "SELECT jina_content, jina_fetched_at "
                        "FROM resource_sources WHERE url = :url"
                    ),
                    {"url": url},
                )
            ).first()
    finally:
        await engine.dispose()

    if row is None:
        return False, None
    content, fetched_at = row
    if not content or not fetched_at:
        return True, None  # Curated but not warmed yet.
    if datetime.now(UTC) - fetched_at > timedelta(days=READ_DB_TTL_DAYS):
        return True, None  # Stale: the read refreshes it.
    return True, content


async def _store(url: str, body: str) -> bool:
    """Persist a fetched body. True if the URL was curated."""
    from sqlalchemy import text

    engine = _engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            result = await conn.execute(
                text(
                    "UPDATE resource_sources "
                    "SET jina_content = :body, jina_fetched_at = :now "
                    "WHERE url = :url"
                ),
                {"body": body, "now": datetime.now(UTC), "url": url},
            )
            return result.rowcount > 0
    finally:
        await engine.dispose()


# Live fetches in flight, by URL (and whether they may log the host): a
# second read of the same cold URL waits for the first fetch rather than
# paying Jina again.
_IN_FLIGHT: dict[tuple[str, bool], asyncio.Task] = {}
_WAITING: dict[tuple[str, bool], int] = {}


async def _fetch(url: str, log_hosts: bool) -> tuple[str | None, str | None]:
    """Jina, then the write-back: (body, None) or (None, error)."""
    async with httpx.AsyncClient() as client:
        result = await jina_read(client, url, log_host=log_hosts)
    if "error" in result:
        return None, result["error"]
    title = result.get("title", "") or ""
    content = result.get("content", "") or ""
    body = f"{title}\n\n{content}".strip()[:READ_MAX_CHARS]
    if not await _store(url, body):
        # Not a curated row: keep it in-process instead.
        _READ_CACHE[url] = (time.time(), body)
    return body, None


async def _shared_fetch(url: str, log_hosts: bool) -> tuple[str | None, str | None]:
    """Join the fetch in flight for ``url``, or start it. A caller that is
    cancelled leaves it running for the others; the last one to leave
    cancels it."""
    key = (url, log_hosts)
    task = _IN_FLIGHT.get(key)
    if task is None:
        task = asyncio.ensure_future(_fetch(url, log_hosts))
        _IN_FLIGHT[key] = task
        task.add_done_callback(
            lambda done: _IN_FLIGHT.pop(key, None)
            if _IN_FLIGHT.get(key) is done
            else None
        )
    _WAITING[key] = _WAITING.get(key, 0) + 1
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if _WAITING.get(key) == 1 and not task.done():
            task.cancel()
        raise
    finally:
        _WAITING[key] -= 1
        if not _WAITING[key]:
            del _WAITING[key]


async def read_corpus(
    url: str,
    *,
    curated_only: bool = False,
    log_hosts: bool = True,
) -> CorpusRead:
    """Read ``url``'s text through the tiered cache.

    A stored body comes back as it was stored; a fresh one is at most
    :data:`READ_MAX_CHARS` characters. ``curated_only`` refuses a URL that
    isn't a ``resource_sources`` row before anything is fetched.
    Simultaneous reads of one cold URL share a single fetch.
    ``log_hosts=False`` keeps the URL's host out of failure logs.
    """
    if not url:
        return CorpusRead(None, "no URL provided", False, False)

    curated, body = await _stored(url)
    if body is not None:
        return CorpusRead(body, None, True, True)
    if curated_only and not curated:
        return CorpusRead(None, "not curated", False, False)

    cached = _READ_CACHE.get(url)
    if cached and (time.time() - cached[0]) < READ_TTL_SECONDS:
        return CorpusRead(cached[1], None, curated, True)

    body, error = await _shared_fetch(url, log_hosts)
    if error is not None:
        return CorpusRead(None, error, curated, False)
    return CorpusRead(body, None, curated, False)
