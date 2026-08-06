"""Give the chat agent a persistent, per-guild memory.

Three tables behind the bot's long- and mid-term memory. ``chat_agent_guild_memory``
holds one markdown blob per guild — what the agent remembers about the place —
rewritten nightly by the dream session and carrying the per-guild kill switch.
``chat_agent_memory_notes`` are the thoughts it keeps mid-conversation via the
``remember`` tool; the dream folds them in and deletes them, so they live under
a day. ``chat_agent_memory_revisions`` is dream-output history, pruned to five
rows per guild and kept out of the blob table so the hot activation read stays
one narrow row.

None of the three is swept by the retention job: the blob and its revisions are
prose the agent authored about itself, not message content it read.

Revision ID: e5a8c2d9f4b1
Revises: d4f7b1c8e2a9
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e5a8c2d9f4b1"
down_revision = "d4f7b1c8e2a9"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def stamps():
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "chat_agent_guild_memory",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("guild_id", sa.String(20), nullable=False),
        sa.Column("content", sa.String(2000), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_dream_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes_consumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        *stamps(),
        sa.UniqueConstraint("guild_id", name="uq_chat_agent_guild_memory_guild_id"),
    )
    op.create_table(
        "chat_agent_memory_notes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("guild_id", sa.String(20), nullable=False),
        sa.Column("channel_id", sa.String(20), nullable=False),
        sa.Column("channel_name", sa.String(120), nullable=True),
        sa.Column("content", sa.String(500), nullable=False),
        # Soft link, deliberately no foreign key: a note must outlive the
        # engagement that produced it and must never be cascade-deleted.
        sa.Column("engagement_id", UUID, nullable=True),
        *stamps(),
    )
    op.create_index(
        "ix_chat_agent_memory_notes_guild_created",
        "chat_agent_memory_notes",
        ["guild_id", "created_at"],
    )
    op.create_table(
        "chat_agent_memory_revisions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("guild_id", sa.String(20), nullable=False),
        sa.Column("content", sa.String(2000), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("notes_consumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_name", sa.String(100), nullable=True),
        *stamps(),
    )
    op.create_index(
        "ix_chat_agent_memory_revisions_guild_revision",
        "chat_agent_memory_revisions",
        ["guild_id", "revision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_agent_memory_revisions_guild_revision",
        table_name="chat_agent_memory_revisions",
    )
    op.drop_table("chat_agent_memory_revisions")
    op.drop_index(
        "ix_chat_agent_memory_notes_guild_created",
        table_name="chat_agent_memory_notes",
    )
    op.drop_table("chat_agent_memory_notes")
    op.drop_table("chat_agent_guild_memory")
