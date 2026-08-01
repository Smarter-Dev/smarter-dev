"""Add Chat-created markdown documents.

Revision ID: f9b2d3e4a5c6
Revises: e8a1c2d3f4b5
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f9b2d3e4a5c6"
down_revision = "e8a1c2d3f4b5"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "web_chat_documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("turn_id", UUID, nullable=False),
        sa.Column("assistant_message_id", UUID, nullable=False),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("markdown_content", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["web_chat_turns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"], ["web_chat_messages.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "turn_id", "tool_call_id", name="uq_web_chat_document_tool_call"
        ),
    )
    op.create_index(
        "ix_web_chat_documents_conversation_created",
        "web_chat_documents",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_web_chat_documents_conversation_created",
        table_name="web_chat_documents",
    )
    op.drop_table("web_chat_documents")
