"""Add ``moderation_shadow_decisions``, the coordinator's shadow record.

One row per message revision the moderation coordinator judged in shadow:
its decision (ids, rule names, labels; never message text) and, filled in a
couple of minutes later, what the legacy actors did to the same message.
Nothing reads it but the shadow comparison. A new table only, so it takes no
lock on anything in use; each statement still waits at most 5 s for a lock.

Revision ID: b7d2e5a8c3f4
Revises: 0a337101a0e8
Create Date: 2026-10-20 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7d2e5a8c3f4"
down_revision: str | None = "0a337101a0e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "moderation_shadow_decisions"


def _bound_lock_waits() -> None:
    # As in e4c8b2f6a9d1: a lock wait fails the migration, never stalls a table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _bound_lock_waits()
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("revision", sa.String(length=80), nullable=False),
        sa.Column("is_edit", sa.Boolean(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False),
        sa.Column("message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=True),
        sa.Column("rules", sa.JSON(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=True),
        sa.Column("classifier", sa.JSON(), nullable=True),
        sa.Column("decision", sa.JSON(), nullable=True),
        sa.Column("strongest_action", sa.String(length=20), nullable=True),
        sa.Column("target_ids", sa.JSON(), nullable=True),
        sa.Column("staff_excluded", sa.JSON(), nullable=True),
        sa.Column("provisional", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("legacy", sa.JSON(), nullable=True),
        sa.Column("legacy_strongest_action", sa.String(length=20), nullable=True),
        sa.Column("legacy_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agreement", sa.String(length=30), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "message_id",
            "revision",
            name="uq_moderation_shadow_decisions_revision",
        ),
    )
    op.create_index(
        "ix_moderation_shadow_decisions_created_at", TABLE, ["created_at"]
    )
    op.create_index(
        "ix_moderation_shadow_decisions_author_id", TABLE, ["author_id"]
    )
    op.create_index(
        "ix_moderation_shadow_decisions_unchecked",
        TABLE,
        ["legacy_checked_at", "created_at"],
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_index("ix_moderation_shadow_decisions_unchecked", table_name=TABLE)
    op.drop_index("ix_moderation_shadow_decisions_author_id", table_name=TABLE)
    op.drop_index("ix_moderation_shadow_decisions_created_at", table_name=TABLE)
    op.drop_table(TABLE)
