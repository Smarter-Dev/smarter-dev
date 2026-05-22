"""Public blog controller (replaces Skrift's auto-registered page-type controller).

Skrift's page-type system only hands the template a list of `Page` rows. We
need richer context — agent/human author chip, reviewer byline, tags — so we
own the routes and the queries here.
"""

from __future__ import annotations

from litestar import Controller, get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.blog_service import (
    BlogPost,
    get_blog_post,
    list_blog_posts,
)


def _featured_post(posts: list[BlogPost]) -> BlogPost | None:
    """Pick the spotlight card. Most recent published post for now."""
    return posts[0] if posts else None


class BlogController(Controller):
    path = "/blog"

    @get("/")
    async def list_view(self, db_session: AsyncSession) -> Template:
        posts = await list_blog_posts(db_session)
        featured = _featured_post(posts)
        grid_posts = [p for p in posts if featured is None or p.id != featured.id]
        return Template(
            "archive-blog.html",
            context={
                "pages": posts,
                "featured": featured,
                "grid_posts": grid_posts,
                "page_type_name": "blog",
                "page_type_plural": "blog",
            },
        )

    @get("/{slug:str}")
    async def detail_view(
        self, db_session: AsyncSession, slug: str
    ) -> Template:
        post = await get_blog_post(db_session, slug)
        if post is None:
            raise NotFoundException(f"Blog post '{slug}' not found")
        return Template(
            "blog.html",
            context={
                "post": post,
                # Keep `page` alias for existing template references.
                "page": post,
                "page_type_name": "blog",
                "page_type_plural": "blog",
            },
        )
