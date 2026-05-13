"""Controller for the /resources/system-architecture page.

Parallel to /resources/agentic-coding-courses but with a 2-level Tools
section: peer tools grouped under decision categories. The spine
(cross-cutting writings/courses) is filterable by learning_type, and the
categories each have an editorial intro followed by a flat tool list.
"""

from __future__ import annotations

import logging

from litestar import get
from litestar.response import Template
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import TrackedLinkCounter
from smarter_dev.web.system_architecture_data import (
    ARCH_CATEGORIES,
    ARCH_FAQS,
    ARCH_PEOPLE,
    ARCH_RESOURCES,
    CATEGORIES,
    ArchResource,
)

logger = logging.getLogger(__name__)

_POPULARITY_THRESHOLD = 10


@get("/resources/system-architecture")
async def system_architecture(db_session: AsyncSession) -> Template:
    counts = await _load_resource_counts(db_session)

    by_latest = sorted(ARCH_RESOURCES, key=lambda r: r.sort_date, reverse=True)
    by_popular = sorted(
        ARCH_RESOURCES, key=lambda r: counts.get(r.key, 0), reverse=True
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

    spine = [_ResourceView(r, counts.get(r.key, 0)) for r in by_latest]
    featured_views = [_ResourceView(r, counts.get(r.key, 0)) for r in featured]

    last_updated = max(r.sort_date for r in ARCH_RESOURCES) if ARCH_RESOURCES else None

    description = (
        "A curated index of writing, courses, and tutorials on architecting "
        "modern systems: how to choose between databases, queues, caches, "
        "proxies, observability stacks, and the rest of the production lineup."
    )

    return Template(
        "system-architecture.html",
        context={
            "categories": ARCH_CATEGORIES,
            "learning_types": CATEGORIES,
            "spine": spine,
            "featured": featured_views,
            "featured_mode": featured_mode,
            "people": ARCH_PEOPLE,
            "faqs": ARCH_FAQS,
            "popularity_unlocked": popularity_unlocked,
            "last_updated": last_updated,
            "seo_meta": {
                "description": description,
                "canonical_url": "https://smarter.dev/resources/system-architecture",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Architecting Modern Systems: A Curated Index",
                "description": description,
                "url": "https://smarter.dev/resources/system-architecture",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )


async def _load_resource_counts(db_session: AsyncSession) -> dict[str, int]:
    """Fetch click counts for arch:* keys, defaulting to empty on missing table."""
    try:
        result = await db_session.execute(
            select(TrackedLinkCounter.key, TrackedLinkCounter.count).where(
                TrackedLinkCounter.key.like("arch:%")
            )
        )
        return {row.key: row.count for row in result}
    except SQLAlchemyError:
        logger.exception(
            "Could not load tracked_link_counters for arch:*; rendering "
            "without click counts."
        )
        await db_session.rollback()
        return {}


class _ResourceView:
    __slots__ = ("_r", "click_count")

    def __init__(self, r: ArchResource, click_count: int) -> None:
        self._r = r
        self.click_count = click_count

    def __getattr__(self, name: str):
        return getattr(self._r, name)
