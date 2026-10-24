"""Add ``moderation_ownership``: who moderates each guild, and since when.

One row per guild that has been switched (``scripts/moderation_cutover.py``).
``owner`` is ``legacy`` (the old actors) or ``coordinator``; ``generation``
goes up on every switch, and the coordinator checks it before every action,
so a rollback fences decisions already under way. ``rules`` are the rules the
coordinator runs; ``legacy_state`` is what the switch turned off, so a
rollback restores exactly that. No row means legacy. A new table only; each
statement still waits at most 5 s for a lock.

Revision ID: f2b6d9c4a1e7
Revises: c5e1f7a3b9d2
Create Date: 2026-10-23 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2b6d9c4a1e7"
down_revision: str | None = "c5e1f7a3b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "moderation_ownership"


def _bound_lock_waits() -> None:
    # As in e4c8b2f6a9d1: a lock wait fails the migration, never stalls a table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _bound_lock_waits()
    op.create_table(
        TABLE,
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(length=20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=True),
        sa.Column("legacy_state", sa.JSON(), nullable=True),
        sa.Column("changed_by", sa.String(length=100), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        # Base gives every model these two.
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
        sa.PrimaryKeyConstraint("guild_id"),
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_table(TABLE)
