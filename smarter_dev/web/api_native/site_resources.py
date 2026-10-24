"""Service-to-service resource metadata lookup for the site's chat answer cards.

The site Worker renders ``sdanswer`` article cards itself. It sends the URLs a
chat answer cites and gets back the catalog fields Python's own renderer reads
(``sdanswer._load_sources`` / ``_source_to_article_card``), so both renderers
show the same title, byline and blurb. Read-only: nothing here writes.
"""

from __future__ import annotations

from typing import Annotated

from litestar import Controller
from litestar import post
from litestar.middleware import DefineMiddleware
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from skrift.auth.guards import Permission
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.rate_limiting import MultiTierRateLimitMiddleware
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.sdanswer import _source_to_article_card

MAX_URLS = 100
MAX_URL_CHARS = 500

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
    middleware = [DefineMiddleware(MultiTierRateLimitMiddleware)]
    # As the other site routes: service responses stay out of shared caches.
    response_headers = {"Cache-Control": "private, no-store"}

    @post("/sources", guards=SITE_RESOURCES_GUARDS, status_code=HTTP_200_OK)
    async def sources(
        self, db_session: AsyncSession, data: SourcesLookup
    ) -> dict[str, list[dict[str, str]]]:
        urls = list(dict.fromkeys(data.urls))
        # Exact URL match, as sdanswer does. URLs are not unique in the
        # catalog, so the lowest track_key wins to keep repeats stable.
        rows = await db_session.scalars(
            select(ResourceSource)
            .where(ResourceSource.url.in_(urls))
            .order_by(ResourceSource.track_key)
        )
        by_url: dict[str, ResourceSource] = {}
        for src in rows:
            by_url.setdefault(src.url, src)
        return {"sources": [_source(by_url[url]) for url in urls if url in by_url]}
