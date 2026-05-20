"""drop blog_posts

The custom BlogPost model has been replaced by Skrift's native page-type
system (`page_types: blog` in app.yaml), which stores blog posts in
`skrift.pages` on the main DB. The legacy `public.blog_posts` table is no
longer referenced.

Revision ID: 10eb241de73b
Revises: 0c69c7839de7
Create Date: 2026-05-20 13:22:36.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "10eb241de73b"
down_revision: Union[str, None] = "0c69c7839de7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_blog_posts_slug"), table_name="blog_posts")
    op.drop_index(op.f("ix_blog_posts_published_at"), table_name="blog_posts")
    op.drop_index("ix_blog_posts_published", table_name="blog_posts")
    op.drop_index(op.f("ix_blog_posts_is_published"), table_name="blog_posts")
    op.drop_index("ix_blog_posts_created_at", table_name="blog_posts")
    op.drop_index("ix_blog_posts_author", table_name="blog_posts")
    op.drop_table("blog_posts")


def downgrade() -> None:
    import sqlalchemy as sa

    op.create_table(
        "blog_posts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=100), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blog_posts")),
        sa.UniqueConstraint("slug", name="uq_blog_posts_slug"),
    )
    op.create_index(
        "ix_blog_posts_author", "blog_posts", ["author"], unique=False
    )
    op.create_index(
        "ix_blog_posts_created_at", "blog_posts", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_blog_posts_is_published"),
        "blog_posts",
        ["is_published"],
        unique=False,
    )
    op.create_index(
        "ix_blog_posts_published",
        "blog_posts",
        ["is_published", "published_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_blog_posts_published_at"),
        "blog_posts",
        ["published_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_blog_posts_slug"), "blog_posts", ["slug"], unique=True
    )
