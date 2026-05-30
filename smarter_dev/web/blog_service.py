"""Read-side service for the blog page-type with its sidecar metadata.

Joins Skrift's `Page` model with the project-owned `author_profiles`,
`blog_post_meta`, and `tags` tables in one query so the controller can hand
the template a ready-shaped listing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class BlogAuthor:
    user_id: UUID | None
    name: str | None
    email: str | None
    is_agent: bool

    @property
    def kind_label(self) -> str:
        return "AGENT" if self.is_agent else "HUMAN"

    @property
    def initials(self) -> str:
        name = self.name or self.email or "?"
        parts = [p for p in name.replace(".", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


@dataclass(slots=True)
class BlogReviewer:
    user_id: UUID
    name: str | None
    email: str | None

    @property
    def initials(self) -> str:
        name = self.name or self.email or "?"
        parts = [p for p in name.replace(".", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


@dataclass(slots=True)
class BlogTag:
    slug: str
    name: str


@dataclass(slots=True)
class BlogPost:
    id: UUID
    slug: str
    title: str
    content: str
    summary: str | None
    published_at: datetime | None
    created_at: datetime
    author: BlogAuthor
    reviewer: BlogReviewer | None
    tags: list[BlogTag] = field(default_factory=list)
    meta_robots: str | None = None

    @property
    def read_minutes(self) -> int:
        # ~220 wpm reading pace; floor at 1 so we never display "0 min".
        words = len(self.content.split()) if self.content else 0
        return max(1, round(words / 220))


_LIST_SQL = text(
    """
    SELECT
        p.id, p.slug, p.title, p.content, p.meta_description AS summary,
        p.meta_robots,
        p.published_at, p.created_at,
        u.id AS author_id, u.name AS author_name, u.email AS author_email,
        COALESCE(ap.is_agent, false) AS author_is_agent,
        ru.id AS reviewer_id, ru.name AS reviewer_name, ru.email AS reviewer_email,
        COALESCE(
            (
                SELECT array_agg(json_build_object('slug', t.slug, 'name', t.name)
                                 ORDER BY t.name)
                  FROM blog_post_tags bpt
                  JOIN tags t ON t.id = bpt.tag_id
                 WHERE bpt.page_id = p.id
            ),
            ARRAY[]::json[]
        ) AS tags
    FROM pages p
    LEFT JOIN users u ON u.id = p.user_id
    LEFT JOIN author_profiles ap ON ap.user_id = p.user_id
    LEFT JOIN blog_post_meta bpm ON bpm.page_id = p.id
    LEFT JOIN users ru ON ru.id = bpm.reviewed_by_user_id
    WHERE p.type = 'blog'
      AND (p.is_published = true OR :include_drafts)
    ORDER BY COALESCE(p.published_at, p.created_at) DESC
    """
)


_DETAIL_SQL = text(
    """
    SELECT
        p.id, p.slug, p.title, p.content, p.meta_description AS summary,
        p.meta_robots,
        p.published_at, p.created_at,
        u.id AS author_id, u.name AS author_name, u.email AS author_email,
        COALESCE(ap.is_agent, false) AS author_is_agent,
        ru.id AS reviewer_id, ru.name AS reviewer_name, ru.email AS reviewer_email,
        COALESCE(
            (
                SELECT array_agg(json_build_object('slug', t.slug, 'name', t.name)
                                 ORDER BY t.name)
                  FROM blog_post_tags bpt
                  JOIN tags t ON t.id = bpt.tag_id
                 WHERE bpt.page_id = p.id
            ),
            ARRAY[]::json[]
        ) AS tags
    FROM pages p
    LEFT JOIN users u ON u.id = p.user_id
    LEFT JOIN author_profiles ap ON ap.user_id = p.user_id
    LEFT JOIN blog_post_meta bpm ON bpm.page_id = p.id
    LEFT JOIN users ru ON ru.id = bpm.reviewed_by_user_id
    WHERE p.type = 'blog' AND p.slug = :slug
      AND (p.is_published = true OR :include_drafts)
    LIMIT 1
    """
)


def _row_to_post(row) -> BlogPost:
    author = BlogAuthor(
        user_id=row.author_id,
        name=row.author_name,
        email=row.author_email,
        is_agent=row.author_is_agent,
    )
    reviewer: BlogReviewer | None = None
    if row.reviewer_id is not None:
        reviewer = BlogReviewer(
            user_id=row.reviewer_id,
            name=row.reviewer_name,
            email=row.reviewer_email,
        )
    tags = [BlogTag(slug=t["slug"], name=t["name"]) for t in (row.tags or [])]
    return BlogPost(
        id=row.id,
        slug=row.slug,
        title=row.title,
        content=row.content,
        summary=row.summary,
        published_at=row.published_at,
        created_at=row.created_at,
        author=author,
        reviewer=reviewer,
        tags=tags,
        meta_robots=row.meta_robots,
    )


async def list_blog_posts(
    db_session: AsyncSession, *, include_drafts: bool = False
) -> list[BlogPost]:
    result = await db_session.execute(
        _LIST_SQL, {"include_drafts": include_drafts}
    )
    return [_row_to_post(row) for row in result]


async def get_blog_post(
    db_session: AsyncSession, slug: str, *, include_drafts: bool = False
) -> BlogPost | None:
    result = await db_session.execute(
        _DETAIL_SQL, {"slug": slug, "include_drafts": include_drafts}
    )
    row = result.first()
    return _row_to_post(row) if row else None
