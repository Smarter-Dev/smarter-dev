"""Controller for /resources/system-architecture.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import load_directory_payload


@get("/resources/system-architecture")
async def system_architecture(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "system-architecture")
    if payload is None:
        raise NotFoundException()

    description = (
        "An index of writing, courses, and tutorials on architecting "
        "modern systems: how to choose between databases, queues, caches, "
        "search engines, APIs, and the rest of the data and integration stack."
    )

    return Template(
        "system-architecture.html",
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
                "canonical_url": "https://smarter.dev/resources/system-architecture",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Architecting Modern Systems: An Index",
                "description": description,
                "url": "https://smarter.dev/resources/system-architecture",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )
