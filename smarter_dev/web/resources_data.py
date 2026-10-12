"""Load /resources/* page content from the database.

Replaces the five hand-maintained Python data modules. One function,
``load_directory_payload``, returns a ``DirectoryPayload`` shaped exactly
the way the existing templates expect: categories with their tool lists,
per-category resource lists, the spine, creators, FAQs, and ``last_updated``.

The view objects carry attribute names compatible with both the arch-style
templates (``r.tool_slugs``, ``r.learning_type``) and the vibe-style template
(``course.tools``, ``course.category``). The cost is a couple of duplicate
attributes per object, paid once at render time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smarter_dev.web.models import ResourceCategory
from smarter_dev.web.models import ResourceDirectory
from smarter_dev.web.models import ResourceDirectorySpine
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.models import ResourceTool
from smarter_dev.web.models import ResourceToolSource
from smarter_dev.web.models import TrackedLinkCounter

# Learning-type taxonomy: shared across every directory, used by the chip
# filter on each "Best Practices / Courses / Discussions" spine section.
LEARNING_TYPES: tuple[str, ...] = (
    "Tutorial",
    "Course",
    "Discussion",
    "Best Practices",
    "Talk",
)


@dataclass(frozen=True)
class SourceView:
    """A learning resource as it appears in templates.

    Field names cover both card partials (arch-style and vibe-style)."""

    title: str
    url: str
    source: str  # byline; templates expect `.source`
    blurb: str
    key: str  # track_key
    learning_type: str
    published_at: date | None
    first_indexed_at: date
    click_count: int = 0
    tool_slugs: tuple[str, ...] = ()  # arch-style attribute name

    @property
    def category(self) -> str:
        """Vibe-style alias for ``learning_type``."""
        return self.learning_type

    @property
    def tools(self) -> tuple[str, ...]:
        """Vibe-style alias for ``tool_slugs``."""
        return self.tool_slugs

    @property
    def category_slug(self) -> str:
        return self.learning_type.lower().replace(" ", "-")

    @property
    def sort_date(self) -> date:
        return self.published_at or self.first_indexed_at


@dataclass(frozen=True)
class ToolView:
    slug: str
    name: str
    url: str
    home_key: str
    blurb: str


@dataclass(frozen=True)
class CategoryView:
    slug: str
    name: str
    intro: str
    tools: tuple[ToolView, ...]


@dataclass(frozen=True)
class CreatorView:
    name: str
    handle: str
    platform: str
    url: str
    key: str
    blurb: str


@dataclass(frozen=True)
class FaqView:
    question: str
    answer: str
    source_label: str
    source_url: str
    source_key: str


@dataclass(frozen=True)
class DirectoryPayload:
    slug: str
    name: str
    track_key_prefix: str
    categories: tuple[CategoryView, ...]
    category_resources: dict[str, list[SourceView]]
    spine: list[SourceView]
    people: list[CreatorView]
    faqs: list[FaqView]
    last_updated: date | None
    learning_types: tuple[str, ...] = LEARNING_TYPES


def build_seed_directory_payload(
    directory: Mapping[str, object],
    page: Mapping[str, object],
    click_counts: Mapping[str, int],
) -> DirectoryPayload:
    """Build the same template payload as the DB loader from seed objects.

    This is used by the static export. The controller context builders below
    then apply the same presentation choices as the live handlers.
    """
    slug = str(directory["slug"])
    is_vibe = slug == "agentic-coding-courses"
    raw_categories = page["categories"]
    if is_vibe:
        raw_categories = (
            SimpleNamespace(
                slug="agentic-tools",
                name="Agentic Tools",
                intro="",
                tools=page["tools"],
            ),
        )

    categories = []
    category_resources = {}
    for category in raw_categories:
        tools = tuple(
            ToolView(
                slug=tool.slug,
                name=tool.name,
                url=tool.url,
                home_key=tool.home_key,
                blurb=getattr(tool, "blurb", None) or getattr(tool, "description", ""),
            )
            for tool in category.tools
        )
        categories.append(
            CategoryView(category.slug, category.name, category.intro, tools)
        )
        tool_slugs = {tool.slug for tool in tools}
        sources = page["courses"] if is_vibe else page["tool_resources"]
        category_resources[category.slug] = [
            _seed_source_view(source, click_counts, matching)
            for source in sources
            if (
                matching := tuple(
                    tool_slug
                    for tool_slug in (source.tools if is_vibe else source.tool_slugs)
                    if tool_slug in tool_slugs
                )
            )
        ]

    raw_spine = (
        [source for source in page["courses"] if not source.tools]
        if is_vibe
        else page["spine"]
    )
    spine = sorted(
        (_seed_source_view(source, click_counts) for source in raw_spine),
        key=lambda source: source.sort_date,
        reverse=True,
    )
    people = [
        CreatorView(
            person.name,
            person.handle,
            person.platform,
            person.url,
            person.key,
            person.blurb,
        )
        for person in page["people"]
    ]
    faqs = [
        FaqView(
            faq.question, faq.answer, faq.source_label, faq.source_url, faq.source_key
        )
        for faq in page["faqs"]
    ]
    dates = [source.sort_date for source in spine]
    dates.extend(
        source.sort_date
        for resources in category_resources.values()
        for source in resources
    )
    return DirectoryPayload(
        slug=slug,
        name=str(directory["name"]),
        track_key_prefix=str(directory["track_key_prefix"]),
        categories=tuple(categories),
        category_resources=category_resources,
        spine=spine,
        people=people,
        faqs=faqs,
        last_updated=max(dates) if dates else None,
    )


def _seed_source_view(source, click_counts, tool_slugs=()) -> SourceView:
    return SourceView(
        title=source.title,
        url=source.url,
        source=source.source,
        blurb=source.blurb or "",
        key=source.key,
        learning_type=getattr(source, "learning_type", None) or source.category,
        published_at=source.published_at,
        first_indexed_at=source.first_indexed_at,
        click_count=click_counts.get(source.key, 0),
        tool_slugs=tool_slugs,
    )


def build_arch_context(
    payload: DirectoryPayload,
    *,
    description: str,
    title: str,
    learning_types: tuple[str, ...] | None = None,
    categories_by_section: list | None = None,
) -> dict:
    """Pure template context shared by the six architecture-style pages."""
    url = f"https://smarter.dev/resources/{payload.slug}"
    context = {
        "categories": payload.categories,
        "category_resources": payload.category_resources,
        "learning_types": learning_types or payload.learning_types,
        "spine": payload.spine,
        "people": payload.people,
        "faqs": payload.faqs,
        "popularity_unlocked": False,
        "last_updated": payload.last_updated,
        "seo_meta": {
            "description": description,
            "canonical_url": url,
            "robots": "index,follow",
        },
        "og_meta": {
            "title": title,
            "description": description,
            "url": url,
            "site_name": "Smarter Dev",
            "type": "article",
            "image": "",
        },
    }
    if categories_by_section is not None:
        context["categories_by_section"] = categories_by_section
    return context


async def load_directory_payload(
    session: AsyncSession, slug: str
) -> DirectoryPayload | None:
    """Load every row needed to render the directory's page.

    Returns ``None`` if the directory doesn't exist yet (handy on first boot
    against a fresh DB before the seed has been applied).
    """
    directory = await _load_directory(session, slug)
    if directory is None:
        return None

    counts = await _load_click_counts(session, directory.track_key_prefix)

    # Build a flat list of categories with their tools. Tools' source lists
    # are loaded as part of the same selectinload chain.
    categories: list[CategoryView] = []
    category_resources: dict[str, list[SourceView]] = {}
    for cat in directory.categories:
        tool_views = tuple(
            ToolView(
                slug=tool.slug,
                name=tool.name,
                url=tool.url,
                home_key=tool.home_track_key,
                blurb=tool.blurb,
            )
            for tool in cat.tools
        )

        # Pass 1: collect every (placement, tool_slug) pair under this
        # category, sorted by placement.sort_order so we render in the
        # original data-file order.
        placements_by_source: dict = {}  # source_id -> list[tool_slug]
        ordered_sources: list = []
        seen_source_ids: set = set()
        all_placements = sorted(
            ((p, tool.slug) for tool in cat.tools for p in tool.source_placements),
            key=lambda item: item[0].sort_order,
        )
        for placement, tool_slug in all_placements:
            placements_by_source.setdefault(placement.source.id, []).append(tool_slug)
            if placement.source.id not in seen_source_ids:
                seen_source_ids.add(placement.source.id)
                ordered_sources.append(placement.source)

        cat_sources = [
            _make_source_view(
                src,
                counts.get(src.track_key, 0),
                tool_slugs=tuple(placements_by_source[src.id]),
            )
            for src in ordered_sources
        ]

        categories.append(
            CategoryView(
                slug=cat.slug,
                name=cat.name,
                intro=cat.intro_html or "",
                tools=tool_views,
            )
        )
        category_resources[cat.slug] = cat_sources

    # Spine: render by published-or-indexed date desc (newest first) — matches
    # the long-standing controller behaviour the templates were tuned against.
    spine_views = [
        _make_source_view(p.source, counts.get(p.source.track_key, 0))
        for p in directory.spine_placements
    ]
    spine = sorted(spine_views, key=lambda s: s.sort_date, reverse=True)

    # Creators
    people = [
        CreatorView(
            name=c.name,
            handle=c.handle,
            platform=c.platform,
            url=c.url,
            key=c.track_key,
            blurb=c.blurb,
        )
        for c in directory.creators
    ]

    # FAQs
    faqs = [
        FaqView(
            question=f.question,
            answer=f.answer,
            source_label=f.source_label,
            source_url=f.source_url,
            source_key=f.source_track_key,
        )
        for f in directory.faqs
    ]

    # Last-updated stamp: newest of all source sort_dates and tool first_indexed_at.
    all_dates: list[date] = []
    for s in spine:
        all_dates.append(s.sort_date)
    for sources in category_resources.values():
        for s in sources:
            all_dates.append(s.sort_date)
    last_updated = max(all_dates) if all_dates else None

    return DirectoryPayload(
        slug=directory.slug,
        name=directory.name,
        track_key_prefix=directory.track_key_prefix,
        categories=tuple(categories),
        category_resources=category_resources,
        spine=spine,
        people=people,
        faqs=faqs,
        last_updated=last_updated,
    )


async def _load_directory(session: AsyncSession, slug: str) -> ResourceDirectory | None:
    stmt = (
        select(ResourceDirectory)
        .where(ResourceDirectory.slug == slug)
        .options(
            selectinload(ResourceDirectory.categories)
            .selectinload(ResourceCategory.tools)
            .selectinload(ResourceTool.source_placements)
            .selectinload(ResourceToolSource.source),
            selectinload(ResourceDirectory.spine_placements).selectinload(
                ResourceDirectorySpine.source
            ),
            selectinload(ResourceDirectory.creators),
            selectinload(ResourceDirectory.faqs),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _load_click_counts(
    session: AsyncSession, track_key_prefix: str
) -> dict[str, int]:
    """One query for every click count under this directory's prefix."""
    try:
        result = await session.execute(
            select(TrackedLinkCounter.key, TrackedLinkCounter.count).where(
                TrackedLinkCounter.key.like(f"{track_key_prefix}:%")
            )
        )
        return {row.key: row.count for row in result}
    except SQLAlchemyError:
        await session.rollback()
        return {}


def _make_source_view(
    source: ResourceSource,
    click_count: int,
    *,
    tool_slugs: tuple[str, ...] = (),
) -> SourceView:
    return SourceView(
        title=source.title,
        url=source.url,
        source=source.byline,
        blurb=source.blurb or "",
        key=source.track_key,
        learning_type=source.learning_type,
        published_at=source.published_at,
        first_indexed_at=source.first_indexed_at,
        click_count=click_count,
        tool_slugs=tool_slugs,
    )
