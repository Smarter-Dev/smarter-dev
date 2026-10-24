"""Service-to-service resource metadata lookup for the site's chat answer cards.

The site Worker renders ``sdanswer`` article cards itself. It sends the URLs a
chat answer cites and gets back the catalog fields Python's own renderer reads
(``sdanswer._load_sources`` / ``_source_to_article_card``), so both renderers
show the same title, byline and blurb, with the same duplicate-URL pick and
field caps. The body is capped at ``MAX_RESPONSE_BYTES``; found sources that
do not fit are listed in ``omitted``. That lookup is read-only.

The chat's research tools (#315, R07) search the catalog and read its
sources here too, the way the Resources researcher does: ``/search`` and
``/read`` call :mod:`smarter_dev.web.resources_corpus`, so the site gets
Python's ranking and the same Jina cache. The search text and the URL travel
in the body, never the request line, and nothing these two routes log names
them: their own lines name the route, and httpx's request lines are held
back while they run. A read is limited to curated rows, bounded in total by
``READ_TIMEOUT_S``, and its body capped at ``READ_MAX_CHARS``. A read that
fetches writes the body back to the cache, as the researcher's does.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Annotated
from typing import Any

from litestar import Controller
from litestar import post
from litestar.serialization import encode_json
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.resources_corpus import READ_MAX_CHARS
from smarter_dev.web.resources_corpus import SEARCH_MAX_LIMIT
from smarter_dev.web.resources_corpus import read_corpus
from smarter_dev.web.resources_corpus import search_corpus
from smarter_dev.web.sdanswer import _load_sources
from smarter_dev.web.sdanswer import _source_to_article_card

MAX_URLS = 100
MAX_URL_CHARS = 500
# The whole encoded JSON body, sources and omitted URLs together.
MAX_RESPONSE_BYTES = 1024 * 1024

SITE_RESOURCES_GUARDS = [*SITE_API_GUARDS, Permission("site-resources")]

logger = logging.getLogger(__name__)

# A whole read (the cache lookup, any live fetch and the write-back) ends
# here, inside the site's own 15 s wait.
READ_TIMEOUT_S = 12.0
MAX_QUERY_CHARS = 500
MAX_READ_URL_CHARS = 2048

# While a corpus route runs, httpx's request lines (which carry the full
# Jina URL) are dropped; everywhere else, the researcher included, they log
# as they always have.
_QUIET = ContextVar("site_corpus_quiet", default=False)


class _QuietWhileCorpusRoutes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET.get()


for _name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11"):
    logging.getLogger(_name).addFilter(_QuietWhileCorpusRoutes())


class CorpusSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(default=8, ge=1, le=SEARCH_MAX_LIMIT)


class CorpusRead(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(min_length=1, max_length=MAX_READ_URL_CHARS)


class SourcesLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[
        Annotated[str, Field(strict=True, min_length=1, max_length=MAX_URL_CHARS)]
    ] = Field(min_length=1, max_length=MAX_URLS)


def _source(src: ResourceSource) -> dict[str, str]:
    # The same fields as the article card sdanswer renders, without its "type".
    card = _source_to_article_card(src)
    card.pop("type")
    return card


class SiteResourcesController(Controller):
    path = "/api/site/resources"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS
    # Each route has its own IP-keyed rule in app.yaml's ``rate_limit``.
    #
    # As the other site routes: service responses stay out of shared caches.
    response_headers = {"Cache-Control": "private, no-store"}

    @post("/sources", guards=SITE_RESOURCES_GUARDS, status_code=HTTP_200_OK)
    async def sources(
        self, db_session: AsyncSession, data: SourcesLookup
    ) -> dict[str, list]:
        urls = list(dict.fromkeys(data.urls))
        # The renderer's own lookup and duplicate-URL pick (exact match).
        by_url = await _load_sources(db_session, set(urls))
        return _bounded([_source(by_url[url]) for url in urls if url in by_url])

    @post("/search", guards=SITE_RESOURCES_GUARDS, status_code=HTTP_200_OK)
    async def search(self, data: CorpusSearch) -> dict[str, Any]:
        _QUIET.set(True)
        return {"hits": await search_corpus(data.query, data.limit)}

    @post("/read", guards=SITE_RESOURCES_GUARDS, status_code=HTTP_200_OK)
    async def read(self, data: CorpusRead) -> dict[str, Any]:
        _QUIET.set(True)
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                read = await read_corpus(data.url, curated_only=True, log_hosts=False)
        except TimeoutError:
            logger.warning("POST /api/site/resources/read: timed out")
            raise plain_error(504, "This source took too long to read.") from None
        if read.error is None:
            return {
                "url": data.url,
                "body": read.body[:READ_MAX_CHARS],
                "cached": read.cached,
            }
        if not read.curated:
            raise plain_error(404, "This isn't one of the curated sources.")
        logger.warning("POST /api/site/resources/read: fetch failed")
        raise plain_error(502, "This source couldn't be read just now.")


def _bounded(found: list[dict[str, str]]) -> dict[str, list]:
    """Sources in request order while the encoded body stays within
    ``MAX_RESPONSE_BYTES``; the URLs of the rest go in ``omitted``.

    A source is always larger than its URL, so the body only grows as sources
    move out of ``omitted``: stopping at the first one that does not fit gives
    the longest prefix that does.
    """
    sizes = [len(encode_json(source)) for source in found]
    url_sizes = [len(encode_json(source["url"])) for source in found]
    fixed = len(encode_json({"sources": [], "omitted": []}))

    def body_size(kept: int) -> int:
        # The two lists' items plus the commas between them.
        shown, rest = sizes[:kept], url_sizes[kept:]
        return (
            fixed
            + sum(shown)
            + max(len(shown) - 1, 0)
            + sum(rest)
            + max(len(rest) - 1, 0)
        )

    kept = 0
    while kept < len(found) and body_size(kept + 1) <= MAX_RESPONSE_BYTES:
        kept += 1
    return {
        "sources": found[:kept],
        "omitted": [source["url"] for source in found[kept:]],
    }
