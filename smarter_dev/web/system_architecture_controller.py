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
    ARCH_TOOL_RESOURCES,
    CATEGORIES,
    ArchResource,
    ArchToolResource,
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

    # Group per-tool resources by their category. A tool's category is the
    # ArchCategory that contains its slug. Resources that touch tools across
    # multiple categories are duplicated under each (rare in practice).
    tool_to_category: dict[str, str] = {}
    for cat in ARCH_CATEGORIES:
        for tool in cat.tools:
            tool_to_category[tool.slug] = cat.slug

    category_resources: dict[str, list[_ToolResourceView]] = {
        cat.slug: [] for cat in ARCH_CATEGORIES
    }
    for r in ARCH_TOOL_RESOURCES:
        cats_seen: set[str] = set()
        for slug in r.tool_slugs:
            cat_slug = tool_to_category.get(slug)
            if cat_slug and cat_slug not in cats_seen:
                category_resources[cat_slug].append(
                    _ToolResourceView(r, counts.get(r.key, 0))
                )
                cats_seen.add(cat_slug)

    last_updated_candidates = [r.sort_date for r in ARCH_RESOURCES]
    last_updated_candidates += [
        (r.published_at or r.first_indexed_at) for r in ARCH_TOOL_RESOURCES
    ]
    last_updated = max(last_updated_candidates) if last_updated_candidates else None

    description = (
        "A curated index of writing, courses, and tutorials on architecting "
        "modern systems: how to choose between databases, queues, caches, "
        "proxies, observability stacks, and the rest of the production lineup."
    )

    return Template(
        "system-architecture.html",
        context={
            "categories": ARCH_CATEGORIES,
            "category_resources": category_resources,
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


class _ToolResourceView:
    __slots__ = ("_r", "click_count")

    def __init__(self, r: ArchToolResource, click_count: int) -> None:
        self._r = r
        self.click_count = click_count

    def __getattr__(self, name: str):
        return getattr(self._r, name)
