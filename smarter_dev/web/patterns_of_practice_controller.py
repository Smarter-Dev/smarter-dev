"""Controller for /resources/patterns-of-practice.

Renders the curated content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import load_directory_payload


@get("/resources/patterns-of-practice")
async def patterns_of_practice(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "patterns-of-practice")
    if payload is None:
        raise NotFoundException()

    description = (
        "The recurring shapes of software, what they cost, and when not to "
        "reach for them. A curated index of design patterns, architectural "
        "patterns, disciplines, anti-patterns, and the new shapes emerging "
        "in the age of agents."
    )

    return Template(
        "patterns-of-practice.html",
        context={
            "categories": payload.categories,
            "category_resources": payload.category_resources,
            "learning_types": payload.learning_types,
            "spine": payload.spine,
            "people": payload.people,
            "faqs": payload.faqs,
            "popularity_unlocked": False,
            "last_updated": payload.last_updated,
            "seo_meta": {
                "description": description,
                "canonical_url": "https://smarter.dev/resources/patterns-of-practice",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Patterns of Practice: A Curated Index",
                "description": description,
                "url": "https://smarter.dev/resources/patterns-of-practice",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )
