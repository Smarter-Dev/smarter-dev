"""Controller for /resources/agentic-coding-courses.

Renders content loaded from the DB. Two structural quirks compared to the
other four directories:

* tools live under one synthetic category (``agentic-tools``); the flat
  ``tools`` list the template wants is that category's ``tools`` tuple.
* creators are split into "Creators to follow" and "Stay current" based on
  ``platform``; the latter is for subscribable feeds (newsletters, podcasts).
"""

from __future__ import annotations

import logging

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Redirect, Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import load_directory_payload

logger = logging.getLogger(__name__)

# Featured strip flips from "Latest" to "Most Popular" once the 3rd-ranked
# tool/workflow source has crossed this threshold.
_POPULARITY_THRESHOLD = 10

# Subscribable platforms get their own section so the "Creators to follow"
# block stays profile-shaped.
_STAY_CURRENT_PLATFORMS: frozenset[str] = frozenset({"newsletter", "podcast"})


@get("/vibe-coding-courses", status_code=301)
async def vibe_courses_legacy_redirect() -> Redirect:
    return Redirect("/resources/agentic-coding-courses", status_code=301)


@get("/resources/vibe-coding-courses", status_code=301)
async def vibe_courses_resources_redirect() -> Redirect:
    return Redirect("/resources/agentic-coding-courses", status_code=301)


@get("/resources/agentic-coding-courses")
async def vibe_courses(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "agentic-coding-courses")
    if payload is None:
        raise NotFoundException()

    # tool_courses = all sources placed under any tool inside the (single)
    # category. workflow_courses = spine.
    tool_courses_sources = list(
        payload.category_resources.get(payload.categories[0].slug, [])
    ) if payload.categories else []
    workflow_courses = list(payload.spine)

    all_courses = tool_courses_sources + workflow_courses

    tool_courses = sorted(tool_courses_sources, key=lambda s: s.sort_date, reverse=True)
    workflow_courses = sorted(workflow_courses, key=lambda s: s.sort_date, reverse=True)

    # Featured strip: top-3 by click count if the threshold is crossed,
    # else the most-recently-indexed three.
    by_popular = sorted(all_courses, key=lambda s: s.click_count, reverse=True)
    popularity_unlocked = (
        len(by_popular) >= 3 and by_popular[2].click_count >= _POPULARITY_THRESHOLD
    )
    if popularity_unlocked:
        featured = by_popular[:3]
        featured_mode = "popular"
    else:
        featured = sorted(all_courses, key=lambda s: s.sort_date, reverse=True)[:3]
        featured_mode = "latest"

    creators = [p for p in payload.people if p.platform not in _STAY_CURRENT_PLATFORMS]
    stay_current = [p for p in payload.people if p.platform in _STAY_CURRENT_PLATFORMS]

    description = (
        "Tutorials, courses, and notes on Claude Code, Cursor, Codex, "
        "Copilot, Lovable, and the rest of this generation of coding "
        "tools. Also known as pair programming, agent mode, or vibe "
        "coding."
    )

    return Template(
        "vibe-coding-courses.html",
        context={
            "tools": payload.categories[0].tools if payload.categories else (),
            "people": creators,
            "stay_current": stay_current,
            # The vibe template's `categories` chip is actually the learning-
            # type chip; keep the existing template field name.
            "categories": payload.learning_types,
            "featured": featured,
            "featured_mode": featured_mode,
            "tool_courses": tool_courses,
            "workflow_courses": workflow_courses,
            "popularity_unlocked": popularity_unlocked,
            "last_updated": payload.last_updated,
            "faqs": payload.faqs,
            "seo_meta": {
                "description": description,
                "canonical_url": "https://smarter.dev/resources/agentic-coding-courses",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Building Software with AI Agents: An Index",
                "description": description,
                "url": "https://smarter.dev/resources/agentic-coding-courses",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )
