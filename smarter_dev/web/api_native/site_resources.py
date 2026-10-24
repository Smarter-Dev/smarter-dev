"""Service-to-service resource metadata lookup for the site's chat answer cards.

The site Worker renders ``sdanswer`` article cards itself. It sends the URLs a
chat answer cites and gets back the catalog fields Python's own renderer reads
(``sdanswer._load_sources`` / ``_source_to_article_card``), so both renderers
show the same title, byline and blurb, with the same duplicate-URL pick and
field caps. The body is capped at ``MAX_RESPONSE_BYTES``; found sources that
do not fit are listed in ``omitted``. Read-only: nothing here writes.
"""

from __future__ import annotations

from typing import Annotated

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
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.sdanswer import _load_sources
from smarter_dev.web.sdanswer import _source_to_article_card

MAX_URLS = 100
MAX_URL_CHARS = 500
# The whole encoded JSON body, sources and omitted URLs together.
MAX_RESPONSE_BYTES = 1024 * 1024

SITE_RESOURCES_GUARDS = [*SITE_API_GUARDS, Permission("site-resources")]


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
    # No per-key rate limiter, as the billing, search, identity and privacy
    # site routes: its bucket is shared with the site memory routes, and the
    # Worker calls this on every chat page load. The read is a small exact match.
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
