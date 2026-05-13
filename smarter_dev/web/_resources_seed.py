"""Seed the /resources/* tables from the legacy Python data modules.

Called once from the Alembic migration that creates the tables, and reusable
as a standalone script when those tables already exist. Idempotent: all
inserts use ON CONFLICT DO UPDATE so re-running the seed reflects the current
state of the Python data files.

After every environment has been seeded, the legacy data modules (and this
file) can be replaced by either an admin UI or a JSON fixture. Until then,
the Python files are the canonical source.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from smarter_dev.web.models import (
    ResourceCategory,
    ResourceCreator,
    ResourceDirectory,
    ResourceDirectorySpine,
    ResourceFaq,
    ResourceSource,
    ResourceTool,
    ResourceToolSource,
)


# Order matches /resources hub: Agentic → Architecture → Hosting → Delivery → Operations.
_DIRECTORIES: list[dict] = [
    {"slug": "agentic-coding-courses", "name": "Agentic Coding",
     "track_key_prefix": "vibe", "sort_order": 10},
    {"slug": "system-architecture", "name": "System Architecture",
     "track_key_prefix": "arch", "sort_order": 20},
    {"slug": "infrastructure-hosting", "name": "Infrastructure & Hosting",
     "track_key_prefix": "infra", "sort_order": 30},
    {"slug": "software-delivery", "name": "Software Delivery",
     "track_key_prefix": "deliv", "sort_order": 40},
    {"slug": "production-operations", "name": "Production Operations",
     "track_key_prefix": "ops", "sort_order": 50},
]


def seed_all(bind: Connection) -> None:
    """Insert every row from the legacy Python data modules into the new tables."""
    # Late imports keep the migration file self-contained while still letting
    # us delete the legacy modules later without touching this file's imports.
    from smarter_dev.web import (
        infrastructure_hosting_data,
        production_operations_data,
        software_delivery_data,
        system_architecture_data,
        vibe_courses_data,
    )

    dir_ids = _seed_directories(bind)

    # ── Agentic Coding (the odd one out: flat tools list, no categories) ──
    vibe_dir_id = dir_ids["agentic-coding-courses"]
    vibe_cat_id = _seed_synthetic_category(
        bind, directory_id=vibe_dir_id, slug="agentic-tools", name="Agentic Tools"
    )
    vibe_tool_ids = _seed_vibe_tools(bind, vibe_cat_id, vibe_courses_data.TOOLS)
    _seed_vibe_courses(
        bind,
        directory_id=vibe_dir_id,
        tool_ids=vibe_tool_ids,
        courses=vibe_courses_data.COURSES,
    )
    _seed_creators(bind, vibe_dir_id, vibe_courses_data.PEOPLE)
    _seed_faqs(bind, vibe_dir_id, vibe_courses_data.FAQS)

    # ── The other four directories share the Arch* dataclass shape ──
    _seed_arch_directory(
        bind,
        directory_id=dir_ids["system-architecture"],
        categories=system_architecture_data.ARCH_CATEGORIES,
        spine=system_architecture_data.ARCH_RESOURCES,
        tool_resources=system_architecture_data.ARCH_TOOL_RESOURCES,
        people=system_architecture_data.ARCH_PEOPLE,
        faqs=system_architecture_data.ARCH_FAQS,
    )
    _seed_arch_directory(
        bind,
        directory_id=dir_ids["infrastructure-hosting"],
        categories=infrastructure_hosting_data.INFRA_CATEGORIES,
        spine=infrastructure_hosting_data.INFRA_SPINE_RESOURCES,
        tool_resources=infrastructure_hosting_data.INFRA_TOOL_RESOURCES,
        people=infrastructure_hosting_data.INFRA_PEOPLE,
        faqs=infrastructure_hosting_data.INFRA_FAQS,
    )
    _seed_arch_directory(
        bind,
        directory_id=dir_ids["software-delivery"],
        categories=software_delivery_data.DELIV_CATEGORIES,
        spine=software_delivery_data.DELIV_SPINE_RESOURCES,
        tool_resources=software_delivery_data.DELIV_TOOL_RESOURCES,
        people=software_delivery_data.DELIV_PEOPLE,
        faqs=software_delivery_data.DELIV_FAQS,
    )
    _seed_arch_directory(
        bind,
        directory_id=dir_ids["production-operations"],
        categories=production_operations_data.OPS_CATEGORIES,
        spine=production_operations_data.OPS_SPINE_RESOURCES,
        tool_resources=production_operations_data.OPS_TOOL_RESOURCES,
        people=production_operations_data.OPS_PEOPLE,
        faqs=production_operations_data.OPS_FAQS,
    )


# ─── Building blocks ────────────────────────────────────────────────────────


def _seed_directories(bind: Connection) -> dict[str, str]:
    """Return {slug: id}, inserting/updating rows."""
    stmt = insert(ResourceDirectory.__table__).values(_DIRECTORIES)
    stmt = stmt.on_conflict_do_update(
        index_elements=["slug"],
        set_={
            "name": stmt.excluded.name,
            "track_key_prefix": stmt.excluded.track_key_prefix,
            "sort_order": stmt.excluded.sort_order,
        },
    )
    bind.execute(stmt)

    rows = bind.execute(
        ResourceDirectory.__table__.select().with_only_columns(
            ResourceDirectory.__table__.c.id,
            ResourceDirectory.__table__.c.slug,
        )
    ).all()
    return {row.slug: row.id for row in rows}


def _seed_synthetic_category(
    bind: Connection, *, directory_id, slug: str, name: str
) -> str:
    stmt = insert(ResourceCategory.__table__).values(
        directory_id=directory_id,
        slug=slug,
        name=name,
        intro_html=None,
        sort_order=10,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_resource_categories_dir_slug",
        set_={
            "name": stmt.excluded.name,
            "intro_html": stmt.excluded.intro_html,
            "sort_order": stmt.excluded.sort_order,
        },
    )
    bind.execute(stmt)
    row = bind.execute(
        ResourceCategory.__table__.select()
        .with_only_columns(ResourceCategory.__table__.c.id)
        .where(ResourceCategory.__table__.c.directory_id == directory_id)
        .where(ResourceCategory.__table__.c.slug == slug)
    ).one()
    return row.id


def _seed_vibe_tools(bind: Connection, category_id, tools: Sequence) -> dict[str, str]:
    """Insert vibe `Tool` rows under the synthetic agentic-tools category."""
    payloads = [
        {
            "category_id": category_id,
            "slug": t.slug,
            "name": t.name,
            "url": t.url,
            "home_track_key": t.home_key,
            "blurb": t.description,
            "sort_order": i * 10,
        }
        for i, t in enumerate(tools)
    ]
    if not payloads:
        return {}
    stmt = insert(ResourceTool.__table__).values(payloads)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_resource_tools_cat_slug",
        set_={
            "name": stmt.excluded.name,
            "url": stmt.excluded.url,
            "home_track_key": stmt.excluded.home_track_key,
            "blurb": stmt.excluded.blurb,
            "sort_order": stmt.excluded.sort_order,
        },
    )
    bind.execute(stmt)
    rows = bind.execute(
        ResourceTool.__table__.select()
        .with_only_columns(
            ResourceTool.__table__.c.id, ResourceTool.__table__.c.slug
        )
        .where(ResourceTool.__table__.c.category_id == category_id)
    ).all()
    return {row.slug: row.id for row in rows}


def _seed_vibe_courses(
    bind: Connection,
    *,
    directory_id,
    tool_ids: dict[str, str],
    courses: Sequence,
) -> None:
    """Insert vibe `Course` rows, splitting them between tool placements and spine."""
    source_id_by_key: dict[str, str] = {}
    spine_courses: list[tuple[int, object]] = []
    tool_placements: list[tuple[object, object, int]] = []

    for i, course in enumerate(courses):
        source_id = _upsert_source(
            bind,
            track_key=course.key,
            title=course.title,
            url=course.url,
            byline=course.source,
            blurb=course.blurb or "",
            learning_type=course.category,
            published_at=course.published_at,
            first_indexed_at=course.first_indexed_at,
        )
        source_id_by_key[course.key] = source_id
        if course.tools:
            for j, tool_slug in enumerate(course.tools):
                if tool_slug in tool_ids:
                    tool_placements.append(
                        (tool_ids[tool_slug], source_id, i * 1000 + j)
                    )
        else:
            spine_courses.append((i * 10, source_id))

    _upsert_directory_spine(bind, directory_id, spine_courses)
    _upsert_tool_sources(bind, tool_placements)


def _seed_arch_directory(
    bind: Connection,
    *,
    directory_id,
    categories,
    spine,
    tool_resources,
    people,
    faqs,
) -> None:
    """Seed one of the four 'Arch'-shaped directories."""
    tool_ids: dict[str, str] = {}

    for cat_i, cat in enumerate(categories):
        cat_id = _upsert_category(
            bind,
            directory_id=directory_id,
            slug=cat.slug,
            name=cat.name,
            intro_html=cat.intro,
            sort_order=cat_i * 10,
        )
        for tool_i, tool in enumerate(cat.tools):
            tid = _upsert_tool(
                bind,
                category_id=cat_id,
                slug=tool.slug,
                name=tool.name,
                url=tool.url,
                home_track_key=tool.home_key,
                blurb=tool.blurb,
                sort_order=tool_i * 10,
            )
            tool_ids[tool.slug] = tid

    spine_rows: list[tuple[int, object]] = []
    for i, r in enumerate(spine):
        source_id = _upsert_source(
            bind,
            track_key=r.key,
            title=r.title,
            url=r.url,
            byline=r.source,
            blurb=r.blurb or "",
            learning_type=r.learning_type,
            published_at=r.published_at,
            first_indexed_at=r.first_indexed_at,
        )
        spine_rows.append((i * 10, source_id))
    _upsert_directory_spine(bind, directory_id, spine_rows)

    # sort_order packs (resource-index-in-file, position-within-tool-slugs-tuple)
    # so the loader can rebuild both the source's category-list position AND
    # the order of slugs in its `data-tools` attribute.
    tool_placements: list[tuple[object, object, int]] = []
    for i, r in enumerate(tool_resources):
        source_id = _upsert_source(
            bind,
            track_key=r.key,
            title=r.title,
            url=r.url,
            byline=r.source,
            blurb=r.blurb or "",
            learning_type=r.learning_type,
            published_at=r.published_at,
            first_indexed_at=r.first_indexed_at,
        )
        for j, tool_slug in enumerate(r.tool_slugs):
            if tool_slug in tool_ids:
                tool_placements.append((tool_ids[tool_slug], source_id, i * 1000 + j))
    _upsert_tool_sources(bind, tool_placements)

    _seed_creators(bind, directory_id, people)
    _seed_faqs(bind, directory_id, faqs)


# ─── Per-table UPSERT helpers ───────────────────────────────────────────────


def _upsert_category(
    bind, *, directory_id, slug, name, intro_html, sort_order
):
    stmt = insert(ResourceCategory.__table__).values(
        directory_id=directory_id,
        slug=slug,
        name=name,
        intro_html=intro_html,
        sort_order=sort_order,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_resource_categories_dir_slug",
        set_={
            "name": stmt.excluded.name,
            "intro_html": stmt.excluded.intro_html,
            "sort_order": stmt.excluded.sort_order,
        },
    )
    bind.execute(stmt)
    row = bind.execute(
        ResourceCategory.__table__.select()
        .with_only_columns(ResourceCategory.__table__.c.id)
        .where(ResourceCategory.__table__.c.directory_id == directory_id)
        .where(ResourceCategory.__table__.c.slug == slug)
    ).one()
    return row.id


def _upsert_tool(
    bind, *, category_id, slug, name, url, home_track_key, blurb, sort_order
):
    stmt = insert(ResourceTool.__table__).values(
        category_id=category_id,
        slug=slug,
        name=name,
        url=url,
        home_track_key=home_track_key,
        blurb=blurb,
        sort_order=sort_order,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_resource_tools_cat_slug",
        set_={
            "name": stmt.excluded.name,
            "url": stmt.excluded.url,
            "home_track_key": stmt.excluded.home_track_key,
            "blurb": stmt.excluded.blurb,
            "sort_order": stmt.excluded.sort_order,
        },
    )
    bind.execute(stmt)
    row = bind.execute(
        ResourceTool.__table__.select()
        .with_only_columns(ResourceTool.__table__.c.id)
        .where(ResourceTool.__table__.c.category_id == category_id)
        .where(ResourceTool.__table__.c.slug == slug)
    ).one()
    return row.id


def _upsert_source(
    bind,
    *,
    track_key,
    title,
    url,
    byline,
    blurb,
    learning_type,
    published_at,
    first_indexed_at,
):
    stmt = insert(ResourceSource.__table__).values(
        track_key=track_key,
        title=title,
        url=url,
        byline=byline,
        blurb=blurb,
        learning_type=learning_type,
        published_at=published_at,
        first_indexed_at=first_indexed_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["track_key"],
        set_={
            "title": stmt.excluded.title,
            "url": stmt.excluded.url,
            "byline": stmt.excluded.byline,
            "blurb": stmt.excluded.blurb,
            "learning_type": stmt.excluded.learning_type,
            "published_at": stmt.excluded.published_at,
            "first_indexed_at": stmt.excluded.first_indexed_at,
        },
    )
    bind.execute(stmt)
    row = bind.execute(
        ResourceSource.__table__.select()
        .with_only_columns(ResourceSource.__table__.c.id)
        .where(ResourceSource.__table__.c.track_key == track_key)
    ).one()
    return row.id


def _upsert_directory_spine(
    bind, directory_id, rows: Iterable[tuple[int, object]]
) -> None:
    payloads = [
        {"directory_id": directory_id, "source_id": source_id, "sort_order": order}
        for order, source_id in rows
    ]
    if not payloads:
        return
    stmt = insert(ResourceDirectorySpine.__table__).values(payloads)
    stmt = stmt.on_conflict_do_update(
        index_elements=["directory_id", "source_id"],
        set_={"sort_order": stmt.excluded.sort_order},
    )
    bind.execute(stmt)


def _upsert_tool_sources(
    bind, rows: Iterable[tuple[object, object, int]]
) -> None:
    payloads = [
        {"tool_id": tool_id, "source_id": source_id, "sort_order": order}
        for tool_id, source_id, order in rows
    ]
    if not payloads:
        return
    stmt = insert(ResourceToolSource.__table__).values(payloads)
    stmt = stmt.on_conflict_do_update(
        index_elements=["tool_id", "source_id"],
        set_={"sort_order": stmt.excluded.sort_order},
    )
    bind.execute(stmt)


def _seed_creators(bind: Connection, directory_id, people: Sequence) -> None:
    payloads = [
        {
            "directory_id": directory_id,
            "slug": _creator_slug(p),
            "name": p.name,
            "handle": p.handle,
            "platform": p.platform,
            "url": p.url,
            "track_key": p.key,
            "blurb": p.blurb,
            "sort_order": i * 10,
        }
        for i, p in enumerate(people)
    ]
    if not payloads:
        return
    stmt = insert(ResourceCreator.__table__).values(payloads)
    stmt = stmt.on_conflict_do_update(
        index_elements=["track_key"],
        set_={
            "name": stmt.excluded.name,
            "handle": stmt.excluded.handle,
            "platform": stmt.excluded.platform,
            "url": stmt.excluded.url,
            "blurb": stmt.excluded.blurb,
            "sort_order": stmt.excluded.sort_order,
            "slug": stmt.excluded.slug,
        },
    )
    bind.execute(stmt)


def _seed_faqs(bind: Connection, directory_id, faqs: Sequence) -> None:
    payloads = [
        {
            "directory_id": directory_id,
            "question": f.question,
            "answer": f.answer,
            "source_label": f.source_label,
            "source_url": f.source_url,
            "source_track_key": f.source_key,
            "sort_order": i * 10,
        }
        for i, f in enumerate(faqs)
    ]
    if not payloads:
        return
    stmt = insert(ResourceFaq.__table__).values(payloads)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_track_key"],
        set_={
            "directory_id": stmt.excluded.directory_id,
            "question": stmt.excluded.question,
            "answer": stmt.excluded.answer,
            "source_label": stmt.excluded.source_label,
            "source_url": stmt.excluded.source_url,
            "sort_order": stmt.excluded.sort_order,
        },
    )
    bind.execute(stmt)


def _creator_slug(person) -> str:
    """Derive a directory-unique slug from the click-key.

    Click-keys look like `arch:person:blog:fowler`. We take the last segment so
    `slug` survives renames of the person's preferred handle.
    """
    return person.key.rsplit(":", 1)[-1]
