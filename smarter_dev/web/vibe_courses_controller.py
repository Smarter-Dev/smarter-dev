"""Controller for the /vibe-coding-courses landing page.

Renders the curated content from ``vibe_courses_data.py`` and merges in click
counts from ``tracked_link_counters``. The featured strip auto-flips from
"Latest" to "Most Popular" once the 3rd-ranked course link has \u226510 clicks;
the "List by popularity" toggle becomes available at that same threshold.

The page has two filterable content sections:

* **Agentic Tools**: resources that teach a specific tool. Filter chips = tool slugs.
* **Workflow & Practice**: cross-cutting resources on the practice of building
  software with coding agents. Filter chips = categories (Tutorial / Course /
  Discussion / Best Practices / Talk).
"""

from __future__ import annotations

import logging

from litestar import get
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from smarter_dev.web.models import TrackedLinkCounter
from smarter_dev.web.vibe_courses_data import (
    CATEGORIES,
    COURSES,
    FAQS,
    PEOPLE,
    STAY_CURRENT_PLATFORMS,
    TOOLS,
    Course,
)

# A featured strip needs at least this many clicks on its 3rd-ranked course
# link before we flip from "Latest" to "Most Popular".
_POPULARITY_THRESHOLD = 10


@get("/vibe-coding-courses", status_code=301)
async def vibe_courses_legacy_redirect() -> Redirect:
    """301 the old URL so any existing inbound links survive the move."""
    return Redirect("/resources/vibe-coding-courses", status_code=301)


@get("/resources/vibe-coding-courses")
async def vibe_courses(db_session: AsyncSession) -> Template:
    counts = await _load_course_counts(db_session)

    by_latest = sorted(COURSES, key=lambda c: c.sort_date, reverse=True)
    by_popular = sorted(
        COURSES, key=lambda c: counts.get(c.key, 0), reverse=True
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

    tool_courses = [
        _CourseView(c, counts.get(c.key, 0)) for c in by_latest if c.tools
    ]
    workflow_courses = [
        _CourseView(c, counts.get(c.key, 0)) for c in by_latest if not c.tools
    ]
    featured_views = [_CourseView(c, counts.get(c.key, 0)) for c in featured]

    description = (
        "Tutorials, courses, and notes on building software with AI coding "
        "agents. Covers Claude Code, Cursor, Codex, Copilot, Lovable, and "
        "more. Also known as AI-assisted development, pair programming, "
        "agent mode, or vibe coding."
    )

    creators = [p for p in PEOPLE if p.platform not in STAY_CURRENT_PLATFORMS]
    stay_current = [p for p in PEOPLE if p.platform in STAY_CURRENT_PLATFORMS]

    # Most-recent indexing date (or publish date, whichever is newer) across
    # all curated resources. Drives the "Last updated" stamp at the bottom.
    last_updated = max(c.sort_date for c in COURSES) if COURSES else None

    return Template(
        "vibe-coding-courses.html",
        context={
            "tools": TOOLS,
            "people": creators,
            "stay_current": stay_current,
            "categories": CATEGORIES,
            "featured": featured_views,
            "featured_mode": featured_mode,
            "tool_courses": tool_courses,
            "workflow_courses": workflow_courses,
            "popularity_unlocked": popularity_unlocked,
            "last_updated": last_updated,
            "faqs": FAQS,
            "seo_meta": {
                "description": description,
                "canonical_url": "https://smarter.dev/resources/vibe-coding-courses",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Building Software with AI Agents: A Curated Index",
                "description": description,
                "url": "https://smarter.dev/resources/vibe-coding-courses",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )


async def _load_course_counts(db_session: AsyncSession) -> dict[str, int]:
    """Fetch click counts for every vibe-coding course key in one query.

    Falls back to an empty dict when the counter table can't be queried (for
    example, when the migration hasn't been applied to a fresh environment
    yet). The page still renders without click counts so the deploy isn't
    blocked on the migration landing.
    """
    try:
        result = await db_session.execute(
            select(TrackedLinkCounter.key, TrackedLinkCounter.count).where(
                TrackedLinkCounter.key.like("vibe:course:%")
            )
        )
        return {row.key: row.count for row in result}
    except SQLAlchemyError:
        logger.exception(
            "Could not load tracked_link_counters; rendering vibe-coding "
            "page without click counts. (Migration applied?)"
        )
        await db_session.rollback()
        return {}


class _CourseView:
    """View wrapper that exposes Course fields plus a live click_count.

    Frozen dataclasses can't be mutated, so we wrap rather than copy. Jinja
    reads attributes off this transparently.
    """

    __slots__ = ("_course", "click_count")

    def __init__(self, course: Course, click_count: int) -> None:
        self._course = course
        self.click_count = click_count

    def __getattr__(self, name: str):
        return getattr(self._course, name)
