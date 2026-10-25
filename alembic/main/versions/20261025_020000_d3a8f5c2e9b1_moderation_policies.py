"""Add the versioned moderation policy (#241).

* ``moderation_policies``: one row per guild, the current ``revision`` and
  its ``document``. No row means the guild still runs on the old config
  tables, so this migration changes no behaviour: the policy is written by
  ``scripts/moderation_policy_import.py``, a separate CRUD Job.
* ``moderation_policy_revisions``: every saved revision.
* ``moderation_policy_loads``: which revision each process has loaded.

New tables only; each statement still waits at most 5 s for a lock.

Revision ID: d3a8f5c2e9b1
Revises: d4a8e2c6f1b3
Create Date: 2026-10-25 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3a8f5c2e9b1"
down_revision: str | None = "d4a8e2c6f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bound_lock_waits() -> None:
    # As in e4c8b2f6a9d1: a lock wait fails the migration, never stalls a table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


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
    _bound_lock_waits()
    op.create_table(
        "moderation_policies",
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("guild_id"),
    )
    op.create_table(
        "moderation_policy_revisions",
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("changed_by", sa.String(length=100), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("guild_id", "revision"),
    )
    op.create_table(
        "moderation_policy_loads",
        sa.Column("process", sa.String(length=200), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("process", "guild_id"),
    )
    op.create_index(
        "ix_moderation_policy_loads_seen_at", "moderation_policy_loads", ["seen_at"]
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_index(
        "ix_moderation_policy_loads_seen_at", table_name="moderation_policy_loads"
    )
    op.drop_table("moderation_policy_loads")
    op.drop_table("moderation_policy_revisions")
    op.drop_table("moderation_policies")
