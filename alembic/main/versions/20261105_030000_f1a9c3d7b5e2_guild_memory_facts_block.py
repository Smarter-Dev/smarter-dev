"""Give guild memory a durable facts block beside behavior and personality

The nightly dream edits a fourth block, ``facts`` (at most 1000 characters):
stable facts about the bot itself, its setup or the server, and what the owner
or an admin decided or corrected. Revisions record it so a bad night can be
read back whole.

The column is added empty on every existing row.

Additive only, so the build before this one keeps working against it: it never
selects the new column, and its upsert leaves it as it is.

Revision ID: f1a9c3d7b5e2
Revises: 201beb972a35
Create Date: 2026-11-05 03:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "f1a9c3d7b5e2"
down_revision: str | None = "201beb972a35"
branch_labels = None
depends_on = None

_TABLES = ("chat_agent_guild_memory", "chat_agent_memory_revisions")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("facts", sa.String(1000), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "facts")
