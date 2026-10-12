"""Controller for /resources/production-operations.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import build_arch_context
from smarter_dev.web.resources_data import load_directory_payload


def build_production_operations_context(payload):
    description = (
        "An index of writing, courses, and tutorials on running "
        "modern systems in production: observability, incident response, "
        "performance, identity, secrets, and network security."
    )
    return build_arch_context(
        payload, description=description, title="Running Modern Systems: An Index"
    )


@get("/resources/production-operations")
async def production_operations(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "production-operations")
    if payload is None:
        raise NotFoundException()
    return Template(
        "production-operations.html",
        context=build_production_operations_context(payload),
    )
