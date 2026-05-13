"""Controller for the /resources/production-operations page.

Early draft. Currently receives Observability + Auth & Secrets from the
System Architecture directory, plus the two spine entries, three creators,
and two FAQs that fit operations rather than design. Incident response,
logging pipelines, the modern auth wave, and secret managers will land
as that work is done.
"""

from __future__ import annotations

import logging

from litestar import get
from litestar.response import Template
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import TrackedLinkCounter
from smarter_dev.web.production_operations_data import (
    OPS_CATEGORIES,
    OPS_FAQS,
    OPS_PEOPLE,
    OPS_SPINE_RESOURCES,
    OPS_TOOL_RESOURCES,
)
from smarter_dev.web.system_architecture_data import (
    CATEGORIES,
    ArchResource,
    ArchToolResource,
)

logger = logging.getLogger(__name__)

_POPULARITY_THRESHOLD = 10


@get("/resources/production-operations")
async def production_operations(db_session: AsyncSession) -> Template:
    counts = await _load_counts(db_session)

    by_latest = sorted(OPS_SPINE_RESOURCES, key=lambda r: r.sort_date, reverse=True)
    by_popular = sorted(
        OPS_SPINE_RESOURCES, key=lambda r: counts.get(r.key, 0), reverse=True
    )

    popularity_unlocked = (
        len(by_popular) >= 3
        and counts.get(by_popular[2].key, 0) >= _POPULARITY_THRESHOLD
    )

    if popularity_unlocked:
        featured = by_popular[:3]
        featured_mode = "popular"
    else:
        featured = by_latest[:3]
        featured_mode = "latest"

    spine = [_SpineView(r, counts.get(r.key, 0)) for r in by_latest]
    featured_views = [_SpineView(r, counts.get(r.key, 0)) for r in featured]

    tool_to_category: dict[str, str] = {}
    for cat in OPS_CATEGORIES:
        for tool in cat.tools:
            tool_to_category[tool.slug] = cat.slug

    category_resources: dict[str, list[_ToolResourceView]] = {
        cat.slug: [] for cat in OPS_CATEGORIES
    }
    for r in OPS_TOOL_RESOURCES:
        seen: set[str] = set()
        for slug in r.tool_slugs:
            cat_slug = tool_to_category.get(slug)
            if cat_slug and cat_slug not in seen:
                category_resources[cat_slug].append(
                    _ToolResourceView(r, counts.get(r.key, 0))
                )
                seen.add(cat_slug)

    candidates = [r.sort_date for r in OPS_SPINE_RESOURCES]
    candidates += [
        (r.published_at or r.first_indexed_at) for r in OPS_TOOL_RESOURCES
    ]
    last_updated = max(candidates) if candidates else None

    description = (
        "Keeping systems healthy in production: observability, auth, "
        "secrets, incident response. Curated docs, tutorials, and best "
        "practices for the running side of software."
    )

    return Template(
        "production-operations.html",
        context={
            "categories": OPS_CATEGORIES,
            "category_resources": category_resources,
            "learning_types": CATEGORIES,
            "spine": spine,
            "featured": featured_views,
            "featured_mode": featured_mode,
            "people": OPS_PEOPLE,
            "faqs": OPS_FAQS,
            "popularity_unlocked": popularity_unlocked,
            "last_updated": last_updated,
            "seo_meta": {
                "description": description,
                "canonical_url": "https://smarter.dev/resources/production-operations",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Production Operations: A Curated Index",
                "description": description,
                "url": "https://smarter.dev/resources/production-operations",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )


async def _load_counts(db_session: AsyncSession) -> dict[str, int]:
    try:
        result = await db_session.execute(
            select(TrackedLinkCounter.key, TrackedLinkCounter.count).where(
                TrackedLinkCounter.key.like("ops:%")
            )
        )
        return {row.key: row.count for row in result}
    except SQLAlchemyError:
        logger.exception("Could not load ops:* counts; rendering without them.")
        await db_session.rollback()
        return {}


class _SpineView:
    __slots__ = ("_r", "click_count")

    def __init__(self, r: ArchResource, click_count: int) -> None:
        self._r = r
        self.click_count = click_count

    def __getattr__(self, name: str):
        return getattr(self._r, name)


class _ToolResourceView:
    __slots__ = ("_r", "click_count")

    def __init__(self, r: ArchToolResource, click_count: int) -> None:
        self._r = r
        self.click_count = click_count

    def __getattr__(self, name: str):
        return getattr(self._r, name)
