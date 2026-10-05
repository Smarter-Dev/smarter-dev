"""Drop candidate_blog_topics

Blog post ideas are gone: the chat agent stopped filing them in July 2026, the
turn endpoint no longer stores any a turn sends, and the blogging pipeline
runs on Scout's news topics alone. This drops the ideas themselves — headline,
observation, scope, evidence and category, with their review status and links
to the engagement, turn, reviewer and page — which can quote members.

Irreversible: downgrade recreates the empty table, not its rows. Nothing
references this table, so no other table changes.

Deploy this with the web build that stops reading the table. A web build from
before it, rolled back without downgrading, fails on the missing table in the
blogging Topics admin page, the pipeline's Review stage, and any turn that
carries ideas (only a bot image from before July 2026 sends them); other
turns are unaffected.

Revision ID: 2b028ca5a19f
Revises: 9c41e07d5b28
Create Date: 2026-10-05 14:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2b028ca5a19f"
down_revision: str | None = "9c41e07d5b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("candidate_blog_topics")


def downgrade() -> None:
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
        sa.Column("observation", sa.Text(), nullable=False),
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
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_blog_topics"),
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["chat_agent_engagements.id"],
            name="fk_candidate_blog_topics_engagement_id_chat_agent_engagements",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["chat_agent_turns.id"],
            name="fk_candidate_blog_topics_turn_id_chat_agent_turns",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name="fk_candidate_blog_topics_reviewed_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["blog_page_id"],
            ["pages.id"],
            name="fk_candidate_blog_topics_blog_page_id_pages",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_candidate_blog_topics_status_surfaced",
        "candidate_blog_topics",
        ["status", "surfaced_at"],
    )
    op.create_index(
        "ix_candidate_blog_topics_engagement_id",
        "candidate_blog_topics",
        ["engagement_id"],
    )
