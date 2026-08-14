"""Mark a conversation as a Quick chat and cut it into threads.

Quick chat is a single perpetual conversation per person: they never start a new
chat, they just keep typing, and when the subject changes a boundary is drawn in
place. ``chat_mode`` says which surface a conversation lives on — deliberately a
different axis from ``intelligence_mode``, which says how hard the agent works,
and unlike that column it is not frozen by ``trg_web_chat_mode_immutable`` (that
trigger names ``intelligence_mode`` explicitly and is left alone here).

``web_chat_threads`` holds the boundaries. A thread is a mark in the scroll, not
a container: no message carries a thread id, membership is derived from
``start_sequence``, and so regeneration and version groups are untouched by
threading. The unique constraint on ``(conversation_id, start_sequence)`` is
load-bearing — it is what makes "open a thread at this turn" idempotent when a
worker retries a turn it already partly processed.

``uq_web_chat_one_live_quick`` is the partial unique index behind the
get-or-create that opens a person's Quick chat: a double-click loses the race
here rather than producing two perpetual chats. Archiving one frees the slot.

Revision ID: b1d6f9c4e8a2
Revises: c9e4b7d2f6a3
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b1d6f9c4e8a2"
down_revision = "c9e4b7d2f6a3"
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
    op.add_column(
        "web_chat_conversations",
        sa.Column(
            "chat_mode",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'standard'"),
        ),
    )
    op.create_check_constraint(
        "web_chat_chat_mode",
        "web_chat_conversations",
        "chat_mode IN ('standard','quick')",
    )
    op.create_index(
        "uq_web_chat_one_live_quick",
        "web_chat_conversations",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("chat_mode = 'quick' AND archived_at IS NULL"),
    )
    op.create_table(
        "web_chat_threads",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start_sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_web_chat_thread_sequence"
        ),
        sa.UniqueConstraint(
            "conversation_id", "start_sequence", name="uq_web_chat_thread_start"
        ),
        sa.CheckConstraint(
            "origin IN ('initial','agent','evaluator')", name="web_chat_thread_origin"
        ),
    )
    op.create_index(
        "ix_web_chat_threads_conversation_start",
        "web_chat_threads",
        ["conversation_id", "start_sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_web_chat_threads_conversation_start", table_name="web_chat_threads"
    )
    op.drop_table("web_chat_threads")
    op.drop_index("uq_web_chat_one_live_quick", table_name="web_chat_conversations")
    # Both ends take the logical name; the metadata naming convention renders it
    # to ck_web_chat_conversations_web_chat_chat_mode on the way to the server.
    op.drop_constraint("web_chat_chat_mode", "web_chat_conversations", type_="check")
    op.drop_column("web_chat_conversations", "chat_mode")
