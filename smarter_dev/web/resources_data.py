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

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smarter_dev.web.models import (
    ResourceCategory,
    ResourceCreator,
    ResourceDirectory,
    ResourceDirectorySpine,
    ResourceFaq,
    ResourceSource,
    ResourceTool,
    ResourceToolSource,
    TrackedLinkCounter,
)


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
    published_at: Optional[date]
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
    last_updated: Optional[date]
    learning_types: tuple[str, ...] = LEARNING_TYPES


async def load_directory_payload(
    session: AsyncSession, slug: str
) -> Optional[DirectoryPayload]:
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
            (
                (p, tool.slug)
                for tool in cat.tools
                for p in tool.source_placements
            ),
            key=lambda item: item[0].sort_order,
        )
        for placement, tool_slug in all_placements:
            placements_by_source.setdefault(
                placement.source.id, []
            ).append(tool_slug)
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


async def _load_directory(
    session: AsyncSession, slug: str
) -> Optional[ResourceDirectory]:
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
