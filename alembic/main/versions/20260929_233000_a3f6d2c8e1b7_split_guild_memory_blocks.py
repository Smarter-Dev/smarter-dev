"""Give guild memory behavior and personality blocks beside the blob

The chat agent's long-term memory row grows two durable blocks the nightly
dream edits alongside ``content``: ``behavior`` (at most 750 characters of
learned instructions for how to act) and ``personality`` (at most 250
characters about itself and how it wants others to feel about it). Revisions
record both so a bad night can be read back whole.

Both columns are added empty on every existing row. ``content`` is not touched:
nothing is reclassified out of an established memory here; the dream moves
what belongs elsewhere gradually, one night at a time.

Additive only, so the build before this one keeps working against it: it
never selects the new columns, and its upsert leaves them as they are.

Revision ID: a3f6d2c8e1b7
Revises: 7c2d9e4b1a60
Create Date: 2026-09-29 23:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a3f6d2c8e1b7"
down_revision: str | None = "7c2d9e4b1a60"
branch_labels = None
depends_on = None

_TABLES = ("chat_agent_guild_memory", "chat_agent_memory_revisions")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("behavior", sa.String(750), nullable=False, server_default=""),
        )
        op.add_column(
            table,
            sa.Column(
                "personality", sa.String(250), nullable=False, server_default=""
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "personality")
        op.drop_column(table, "behavior")
