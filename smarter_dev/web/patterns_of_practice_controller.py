"""Controller for /resources/patterns-of-practice.

Renders the content loaded from the DB (see ``resources_data``).

Builds an H2-grouped view: categories are rendered under section headings,
with the section map living in the Python data module rather than the DB so
the seed schema can stay shared across all six directories.
"""

from __future__ import annotations

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.patterns_of_practice_data import POP_SECTIONS_BY_SLUG
from smarter_dev.web.resources_data import load_directory_payload


# Order matters: H2 sections render in this order. Any section a category
# slug maps to via ``POP_SECTIONS_BY_SLUG`` must appear here.
POP_SECTION_ORDER: tuple[str, ...] = (
    "Code Patterns",
    "Architecture Patterns",
    "Patterns of Discipline",
    "Anti-Patterns",
)


# PoP doesn't use Course-shaped entries, so the chip would always filter to
# nothing. Restrict the chip set to the types that actually appear.
POP_LEARNING_TYPES: tuple[str, ...] = (
    "Tutorial",
    "Discussion",
    "Best Practices",
    "Talk",
)


@get("/resources/patterns-of-practice")
async def patterns_of_practice(db_session: AsyncSession) -> Template:
    payload = await load_directory_payload(db_session, "patterns-of-practice")
    if payload is None:
        raise NotFoundException()

    grouped = _group_categories_by_section(payload.categories)

    description = (
        "The recurring shapes of software, what they cost, and when not to "
        "reach for them. Design patterns, architectural patterns, "
        "disciplines, and the anti-patterns worth refusing on purpose."
    )

    return Template(
        "patterns-of-practice.html",
        context={
            "categories": payload.categories,
            "categories_by_section": grouped,
            "category_resources": payload.category_resources,
            "learning_types": POP_LEARNING_TYPES,
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
                "title": "Patterns of Practice: A Working Index",
                "description": description,
                "url": "https://smarter.dev/resources/patterns-of-practice",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )


def _group_categories_by_section(categories):
    """Return ``[(section_name, [CategoryView, ...]), ...]`` in display order."""
    buckets: dict[str, list] = {name: [] for name in POP_SECTION_ORDER}
    for cat in categories:
        section = POP_SECTIONS_BY_SLUG.get(cat.slug)
        if section is None or section not in buckets:
            # Defensive: surface an unmapped category at the top rather than
            # silently dropping it. Easier to spot in dev than via empty H2s.
            buckets.setdefault("Unfiled", []).append(cat)
            continue
        buckets[section].append(cat)
    return [(name, cats) for name, cats in buckets.items() if cats]
