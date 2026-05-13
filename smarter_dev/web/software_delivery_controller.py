"""Controller for /resources/software-delivery.

Mirrors the production_operations controller's shape: spine resources with
chip filtering, per-category tool resources, creators, FAQs.
"""

from __future__ import annotations

import logging

from litestar import get
from litestar.response import Template
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import TrackedLinkCounter
from smarter_dev.web.software_delivery_data import (
    DELIV_CATEGORIES,
    DELIV_FAQS,
    DELIV_PEOPLE,
    DELIV_SPINE_RESOURCES,
    DELIV_TOOL_RESOURCES,
)
from smarter_dev.web.system_architecture_data import (
    CATEGORIES,
    ArchResource,
    ArchToolResource,
)

logger = logging.getLogger(__name__)

_POPULARITY_THRESHOLD = 10


@get("/resources/software-delivery")
async def software_delivery(db_session: AsyncSession) -> Template:
    counts = await _load_counts(db_session)

    by_latest = sorted(DELIV_SPINE_RESOURCES, key=lambda r: r.sort_date, reverse=True)
    by_popular = sorted(
        DELIV_SPINE_RESOURCES, key=lambda r: counts.get(r.key, 0), reverse=True
    )

    popularity_unlocked = (
        len(by_popular) >= 3
        and counts.get(by_popular[2].key, 0) >= _POPULARITY_THRESHOLD
    )

    spine = [_SpineView(r, counts.get(r.key, 0)) for r in by_latest]

    tool_to_category: dict[str, str] = {}
    for cat in DELIV_CATEGORIES:
        for tool in cat.tools:
            tool_to_category[tool.slug] = cat.slug

    category_resources: dict[str, list[_ToolResourceView]] = {
        cat.slug: [] for cat in DELIV_CATEGORIES
    }
    for r in DELIV_TOOL_RESOURCES:
        seen: set[str] = set()
        for slug in r.tool_slugs:
            cat_slug = tool_to_category.get(slug)
            if cat_slug and cat_slug not in seen:
                category_resources[cat_slug].append(
                    _ToolResourceView(r, counts.get(r.key, 0))
                )
                seen.add(cat_slug)

    candidates = [r.sort_date for r in DELIV_SPINE_RESOURCES]
    candidates += [
        (r.published_at or r.first_indexed_at) for r in DELIV_TOOL_RESOURCES
    ]
    last_updated = max(candidates) if candidates else None

    description = (
        "A curated index of writing, courses, and tutorials on shipping "
        "software: version control, CI/CD, infrastructure-as-code, "
        "deployment, container builds, local dev, database migrations, "
        "and feature flags."
    )

    return Template(
        "software-delivery.html",
        context={
            "categories": DELIV_CATEGORIES,
            "category_resources": category_resources,
            "learning_types": CATEGORIES,
            "spine": spine,
            "people": DELIV_PEOPLE,
            "faqs": DELIV_FAQS,
            "popularity_unlocked": popularity_unlocked,
            "last_updated": last_updated,
            "seo_meta": {
                "description": description,
                "canonical_url": "https://smarter.dev/resources/software-delivery",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Shipping Modern Systems: A Curated Index",
                "description": description,
                "url": "https://smarter.dev/resources/software-delivery",
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
                TrackedLinkCounter.key.like("deliv:%")
            )
        )
        return {row.key: row.count for row in result}
    except SQLAlchemyError:
        logger.exception("Could not load deliv:* counts; rendering without them.")
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
