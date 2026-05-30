"""reshape candidate_blog_topics for hypothesis-driven pipeline

Drops the editorial ``pitch`` framing in favour of neutral ``observation``
+ ``scope`` + ``evidence`` — same shape Scout returns. The chat agent
(stage 1 capture) and Scout (stage 2 capture) now produce identical
candidate shapes, and Brainstorm forms hypotheses from claims rather
than picking from pre-editorialised pitches.

Existing rows are wiped — they were all local-dev seed data; no real
captures exist in any deployed environment yet.

Revision ID: 0d0a8f9e6096
Revises: 1a4b4df89a32
Create Date: 2026-05-22 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0d0a8f9e6096"
down_revision: Union[str, None] = "1a4b4df89a32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop pre-rename rows; they have no observation/scope/evidence to
    # backfill cleanly.
    op.execute("TRUNCATE candidate_blog_topics")
    op.alter_column(
        "candidate_blog_topics",
        "pitch",
        new_column_name="observation",
    )
    op.add_column(
        "candidate_blog_topics",
        sa.Column(
            "scope",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "candidate_blog_topics",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("candidate_blog_topics", "evidence")
    op.drop_column("candidate_blog_topics", "scope")
    op.alter_column(
        "candidate_blog_topics",
        "observation",
        new_column_name="pitch",
    )
