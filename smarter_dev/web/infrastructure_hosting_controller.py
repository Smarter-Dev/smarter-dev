"""Controller for /resources/infrastructure-hosting.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import build_arch_context
from smarter_dev.web.resources_data import load_directory_payload


def build_infrastructure_hosting_context(payload):
    description = (
        "An index of writing, courses, and tutorials on hosting "
        "modern systems: cloud providers, PaaS, managed data services, "
        "containers, orchestration, networking, and the rest of the "
        "production substrate."
    )
    return build_arch_context(
        payload, description=description, title="Hosting Modern Systems: An Index"
    )


@get("/resources/infrastructure-hosting")
async def infrastructure_hosting(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "infrastructure-hosting")
    if payload is None:
        raise NotFoundException()
    return Template(
        "infrastructure-hosting.html",
        context=build_infrastructure_hosting_context(payload),
    )
