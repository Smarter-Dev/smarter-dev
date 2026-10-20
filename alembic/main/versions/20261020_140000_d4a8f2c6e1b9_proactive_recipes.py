"""Add ``proactive_recipes`` and ``proactive_recipe_settings`` (#267).

Admin-written recipes the proactive agent can open on demand, and each
guild's preview length. New tables only, so nothing in use is locked; each
statement still waits at most 5 s for a lock.

Revision ID: d4a8f2c6e1b9
Revises: b7d2e5a8c3f4
Create Date: 2026-10-20 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4a8f2c6e1b9"
down_revision: str | None = "b7d2e5a8c3f4"
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
        "proactive_recipes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("name_key", sa.String(length=80), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_proactive_recipes"),
        sa.UniqueConstraint("guild_id", "name_key", name="uq_proactive_recipes_name"),
    )
    op.create_table(
        "proactive_recipe_settings",
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("preview_chars", sa.Integer(), server_default="300", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("guild_id", name="pk_proactive_recipe_settings"),
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_table("proactive_recipe_settings")
    op.drop_table("proactive_recipes")
