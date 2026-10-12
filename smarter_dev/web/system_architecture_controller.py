"""Controller for /resources/system-architecture.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import build_arch_context
from smarter_dev.web.resources_data import load_directory_payload


def build_system_architecture_context(payload):
    description = (
        "An index of writing, courses, and tutorials on architecting "
        "modern systems: how to choose between databases, queues, caches, "
        "search engines, APIs, and the rest of the data and integration stack."
    )
    return build_arch_context(
        payload, description=description, title="Architecting Modern Systems: An Index"
    )


@get("/resources/system-architecture")
async def system_architecture(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "system-architecture")
    if payload is None:
        raise NotFoundException()
    return Template(
        "system-architecture.html", context=build_system_architecture_context(payload)
    )
