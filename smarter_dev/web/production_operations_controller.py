"""Controller for /resources/production-operations.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import load_directory_payload


@get("/resources/production-operations")
async def production_operations(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "production-operations")
    if payload is None:
        raise NotFoundException()

    description = (
        "An index of writing, courses, and tutorials on running "
        "modern systems in production: observability, incident response, "
        "performance, identity, secrets, and network security."
    )

    return Template(
        "production-operations.html",
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
                "canonical_url": "https://smarter.dev/resources/production-operations",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Running Modern Systems: An Index",
                "description": description,
                "url": "https://smarter.dev/resources/production-operations",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )
