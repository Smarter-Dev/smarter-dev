"""jam teams, their members and showcase submissions

#497 (2026-11-24). ``jam_teams`` holds one row per team in a jam, with the id
of its private thread. ``jam_team_members`` holds the roster; the pair
(jam, member) is unique, so a member is on one team per jam.
``jam_submissions`` holds one row per showcase post made while a jam took
submissions, linked to the author's team if they had one.

All three tables are new, so the previous build never reads them and a
rollback only has to drop them.

Revision ID: a7d3f1c9e2b6
Revises: e5b1d8a3c7f4
Create Date: 2026-11-24 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7d3f1c9e2b6"
down_revision: str | None = "e5b1d8a3c7f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    # Base gives every model these two.
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "jam_teams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jam_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("thread_id", sa.String(length=22), nullable=False),
        sa.Column("created_by", sa.String(length=22), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jam_teams")),
        sa.ForeignKeyConstraint(
            ["jam_id"],
            ["jams.id"],
            name=op.f("fk_jam_teams_jam_id_jams"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("thread_id", name="uq_jam_teams_thread_id"),
        sa.UniqueConstraint("id", "jam_id", name="uq_jam_teams_id_jam_id"),
    )
    op.create_index("ix_jam_teams_jam_id", "jam_teams", ["jam_id"])
    op.create_table(
        "jam_team_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jam_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=22), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jam_team_members")),
        sa.ForeignKeyConstraint(
            ["team_id", "jam_id"],
            ["jam_teams.id", "jam_teams.jam_id"],
            name="fk_jam_team_members_team_id_jam_id_jam_teams",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "jam_id", "user_id", name="uq_jam_team_members_jam_id_user_id"
        ),
    )
    op.create_index("ix_jam_team_members_team_id", "jam_team_members", ["team_id"])
    op.create_table(
        "jam_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jam_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=22), nullable=False),
        sa.Column("author_id", sa.String(length=22), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jam_submissions")),
        sa.ForeignKeyConstraint(
            ["jam_id"],
            ["jams.id"],
            name=op.f("fk_jam_submissions_jam_id_jams"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["jam_teams.id"],
            name=op.f("fk_jam_submissions_team_id_jam_teams"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("thread_id", name="uq_jam_submissions_thread_id"),
    )
    op.create_index("ix_jam_submissions_jam_id", "jam_submissions", ["jam_id"])


def downgrade() -> None:
    op.drop_table("jam_submissions")
    op.drop_table("jam_team_members")
    op.drop_table("jam_teams")
