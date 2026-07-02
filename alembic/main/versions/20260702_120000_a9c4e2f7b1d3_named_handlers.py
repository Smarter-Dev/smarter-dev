"""Named handlers: multiple per (channel, trigger), each with a unique name.

Adds ``name`` to channel_handlers (unique per channel) and admin_handlers
(unique per guild), backfilled as ``<trigger>-<first 8 of id>`` for existing
rows. Drops the single-listener partial unique index — any number of event
handlers may now share a (channel, trigger) and every enabled one fires.

Revision ID: a9c4e2f7b1d3
Revises: f1a3d6c8b2e9
Create Date: 2026-07-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9c4e2f7b1d3"
down_revision: Union[str, None] = "f1a3d6c8b2e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channel_handlers", sa.Column("name", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE channel_handlers "
        "SET name = trigger_type || '-' || substr(CAST(id AS TEXT), 1, 8)"
    )
    op.alter_column("channel_handlers", "name", nullable=False)
    op.drop_index("uq_channel_handlers_event_listener", table_name="channel_handlers")
    op.create_index(
        "uq_channel_handlers_channel_name",
        "channel_handlers",
        ["channel_id", "name"],
        unique=True,
    )

    op.add_column(
        "admin_handlers", sa.Column("name", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE admin_handlers "
        "SET name = trigger_type || '-' || substr(CAST(id AS TEXT), 1, 8)"
    )
    op.alter_column("admin_handlers", "name", nullable=False)
    op.create_index(
        "uq_admin_handlers_guild_name",
        "admin_handlers",
        ["guild_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_admin_handlers_guild_name", table_name="admin_handlers")
    op.drop_column("admin_handlers", "name")

    op.drop_index(
        "uq_channel_handlers_channel_name", table_name="channel_handlers"
    )
    # Restore the single-listener invariant; keep the newest event handler per
    # (channel, trigger) and drop the rest so the unique index can be built.
    op.execute(
        "DELETE FROM channel_handlers WHERE trigger_type IN ('message', 'reaction') "
        "AND id NOT IN ("
        "  SELECT id FROM ("
        "    SELECT DISTINCT ON (channel_id, trigger_type) id FROM channel_handlers "
        "    WHERE trigger_type IN ('message', 'reaction') "
        "    ORDER BY channel_id, trigger_type, id DESC"
        "  ) AS keep"
        ")"
    )
    op.drop_column("channel_handlers", "name")
    op.create_index(
        "uq_channel_handlers_event_listener",
        "channel_handlers",
        ["channel_id", "trigger_type"],
        unique=True,
        postgresql_where=sa.text("trigger_type IN ('message', 'reaction')"),
        sqlite_where=sa.text("trigger_type IN ('message', 'reaction')"),
    )
