"""jam results: reaction counts and when a jam's closing steps ran

#497 (2026-11-25). ``jams.teams_closed_at`` is set when the bot has told the
teams that submissions closed and locked their threads.
``jams.votes_counted_at`` is set when it has counted the reactions on the
entries after voting ended; the counts go in
``jam_submissions.reaction_count`` and ``reactor_count``.
``jam_submissions.removed_at`` marks a post an administrator stopped counting;
before this the row was deleted, and the bot would now find the post in the
forum and record it again.

All five columns are nullable with no default, so the previous build neither
reads nor has to write them, and a rollback only has to drop them.

Revision ID: c4e8a2f6d1b9
Revises: a7d3f1c9e2b6
Create Date: 2026-11-25 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8a2f6d1b9"
down_revision: str | None = "a7d3f1c9e2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jams",
        sa.Column("teams_closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jams",
        sa.Column("votes_counted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jam_submissions",
        sa.Column("reaction_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "jam_submissions",
        sa.Column("reactor_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "jam_submissions",
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jam_submissions", "removed_at")
    op.drop_column("jam_submissions", "reactor_count")
    op.drop_column("jam_submissions", "reaction_count")
    op.drop_column("jams", "votes_counted_at")
    op.drop_column("jams", "teams_closed_at")
