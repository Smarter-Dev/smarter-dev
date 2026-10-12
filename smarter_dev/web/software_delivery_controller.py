"""Controller for /resources/software-delivery.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import build_arch_context
from smarter_dev.web.resources_data import load_directory_payload


def build_software_delivery_context(payload):
    description = (
        "An index of writing, courses, and tutorials on shipping "
        "software: version control, CI/CD, infrastructure-as-code, "
        "deployment, container builds, local dev, database migrations, "
        "and feature flags."
    )
    return build_arch_context(
        payload, description=description, title="Shipping Modern Systems: An Index"
    )


@get("/resources/software-delivery")
async def software_delivery(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "software-delivery")
    if payload is None:
        raise NotFoundException()
    return Template(
        "software-delivery.html", context=build_software_delivery_context(payload)
    )
