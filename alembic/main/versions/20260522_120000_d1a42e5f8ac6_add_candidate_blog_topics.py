"""add candidate_blog_topics

Stage 1 of the blogging-agent pipeline: the Discord chat agent surfaces
blog-post ideas during conversations; this table is where they land for
human triage in the admin.

`engagement_id` / `turn_id` are nullable so an idea isn't lost if its source
conversation is deleted. `reviewed_by_user_id` / `blog_page_id` are FKs into
the Skrift package's tables (users / pages), declared via raw SQL since the
Skrift models live on a separate Base metadata.

Revision ID: d1a42e5f8ac6
Revises: 8cda71bbf6f2
Create Date: 2026-05-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1a42e5f8ac6"
down_revision: Union[str, None] = "8cda71bbf6f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_blog_topics",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=True),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column(
            "surfaced_by",
            sa.String(length=32),
            nullable=False,
            server_default="chat-agent",
        ),
        sa.Column(
            "surfaced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("headline", sa.String(length=255), nullable=False),
        sa.Column("pitch", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="new",
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("blog_page_id", sa.UUID(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_blog_topics")),
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["chat_agent_engagements.id"],
            name=op.f(
                "fk_candidate_blog_topics_engagement_id_chat_agent_engagements"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["chat_agent_turns.id"],
            name=op.f("fk_candidate_blog_topics_turn_id_chat_agent_turns"),
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_candidate_blog_topics_status_surfaced",
        "candidate_blog_topics",
        ["status", "surfaced_at"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_blog_topics_engagement_id",
        "candidate_blog_topics",
        ["engagement_id"],
        unique=False,
    )
    op.execute(
        "ALTER TABLE candidate_blog_topics "
        "ADD CONSTRAINT fk_candidate_blog_topics_reviewed_by_user_id_users "
        "FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id) "
        "ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE candidate_blog_topics "
        "ADD CONSTRAINT fk_candidate_blog_topics_blog_page_id_pages "
        "FOREIGN KEY (blog_page_id) REFERENCES pages(id) "
        "ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_blog_topics_engagement_id",
        table_name="candidate_blog_topics",
    )
    op.drop_index(
        "ix_candidate_blog_topics_status_surfaced",
        table_name="candidate_blog_topics",
    )
    op.drop_table("candidate_blog_topics")
