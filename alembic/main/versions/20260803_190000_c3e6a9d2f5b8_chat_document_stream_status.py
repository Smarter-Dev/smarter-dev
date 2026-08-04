"""Track write status for streamed Chat documents.

Documents are now written a delta at a time, so a row is visible before it is
finished. Existing rows were written whole, hence the "complete" backfill.

Revision ID: c3e6a9d2f5b8
Revises: b2d5f8c1e4a7
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c3e6a9d2f5b8"
down_revision = "b2d5f8c1e4a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_chat_documents",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="complete",
        ),
    )
    op.create_index(
        "ix_web_chat_documents_conversation_filename",
        "web_chat_documents",
        ["conversation_id", "filename"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_web_chat_documents_conversation_filename",
        table_name="web_chat_documents",
    )
    op.drop_column("web_chat_documents", "status")
