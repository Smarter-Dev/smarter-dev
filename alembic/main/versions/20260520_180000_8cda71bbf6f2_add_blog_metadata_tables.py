"""add blog metadata tables

Adds project-owned sidecar tables for the blog page-type:

- ``author_profiles`` — per-user flag for agent-authored vs human-authored.
- ``blog_post_meta`` — reviewer FK (and room for future per-post fields)
  for a Skrift page with ``type='blog'``.
- ``tags`` + ``blog_post_tags`` — many-to-many taxonomy tags.

These live in this project rather than Skrift core because they're
smarter.dev workflow concepts (agent authorship, review attribution, blog
tagging), not generic CMS features.

FKs into ``skrift.users`` / ``skrift.pages`` are declared via raw SQL since
the Skrift models live on a separate Base metadata.

Revision ID: 8cda71bbf6f2
Revises: b7e9d2c4f1a8
Create Date: 2026-05-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8cda71bbf6f2"
down_revision: Union[str, None] = "b7e9d2c4f1a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "author_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "is_agent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_author_profiles")),
        sa.UniqueConstraint("user_id", name="uq_author_profiles_user_id"),
    )
    op.execute(
        "ALTER TABLE author_profiles "
        "ADD CONSTRAINT fk_author_profiles_user_id_users "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
    )

    op.create_table(
        "blog_post_meta",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_id", sa.UUID(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.UUID(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blog_post_meta")),
        sa.UniqueConstraint("page_id", name="uq_blog_post_meta_page_id"),
    )
    op.execute(
        "ALTER TABLE blog_post_meta "
        "ADD CONSTRAINT fk_blog_post_meta_page_id_pages "
        "FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE blog_post_meta "
        "ADD CONSTRAINT fk_blog_post_meta_reviewed_by_user_id_users "
        "FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id) "
        "ON DELETE SET NULL"
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("slug", name="uq_tags_slug"),
    )
    op.create_index(op.f("ix_tags_slug"), "tags", ["slug"], unique=True)

    op.create_table(
        "blog_post_tags",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("page_id", sa.UUID(), nullable=False),
        sa.Column("tag_id", sa.UUID(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blog_post_tags")),
        sa.UniqueConstraint(
            "page_id", "tag_id", name="uq_blog_post_tags_page_tag"
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name=op.f("fk_blog_post_tags_tag_id_tags"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_blog_post_tags_page_id", "blog_post_tags", ["page_id"], unique=False
    )
    op.execute(
        "ALTER TABLE blog_post_tags "
        "ADD CONSTRAINT fk_blog_post_tags_page_id_pages "
        "FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    op.drop_index("ix_blog_post_tags_page_id", table_name="blog_post_tags")
    op.drop_table("blog_post_tags")
    op.drop_index(op.f("ix_tags_slug"), table_name="tags")
    op.drop_table("tags")
    op.drop_table("blog_post_meta")
    op.drop_table("author_profiles")
