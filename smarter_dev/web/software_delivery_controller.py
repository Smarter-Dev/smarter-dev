"""Controller for /resources/software-delivery.

Renders the curated content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import load_directory_payload


@get("/resources/software-delivery")
async def software_delivery(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "software-delivery")
    if payload is None:
        raise NotFoundException()

    description = (
        "A curated index of writing, courses, and tutorials on shipping "
        "software: version control, CI/CD, infrastructure-as-code, "
        "deployment, container builds, local dev, database migrations, "
        "and feature flags."
    )

    return Template(
        "software-delivery.html",
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
