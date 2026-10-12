"""Controller for /resources/agent-engineering-patterns.

Renders the content loaded from the DB (see ``resources_data``).
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.resources_data import build_arch_context
from smarter_dev.web.resources_data import load_directory_payload

AGENT_LEARNING_TYPES: tuple[str, ...] = (
    "Tutorial",
    "Discussion",
    "Best Practices",
    "Talk",
)


def build_agent_engineering_patterns_context(payload):
    description = (
        "Patterns for the Age of Agents. How a codebase is shaped so an "
        "agent can do useful work, how that work is verified at machine "
        "speed, and how humans stay in the decision path without becoming "
        "the bottleneck."
    )
    return build_arch_context(
        payload,
        description=description,
        title="Patterns for the Age of Agents",
        learning_types=AGENT_LEARNING_TYPES,
    )


@get("/resources/agent-engineering-patterns")
async def agent_engineering_patterns(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "agent-engineering-patterns")
    if payload is None:
        raise NotFoundException()
    return Template(
        "agent-engineering-patterns.html",
        context=build_agent_engineering_patterns_context(payload),
    )
