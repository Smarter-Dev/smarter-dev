"""Let a Chat conversation be named and archived.

The agent names a conversation on its first turn and the owner can rename it,
so a title now records whether anyone chose it — an unchosen one is still the
first message's opening words and may be overwritten. Archiving takes a
conversation out of the history rail without destroying it.

Revision ID: d4f7b1c8e2a9
Revises: c3e6a9d2f5b8
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4f7b1c8e2a9"
down_revision = "c3e6a9d2f5b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_chat_conversations",
        sa.Column(
            "title_is_custom",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "web_chat_conversations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Every conversation that already has a title got it before the agent could
    # name one, and it is already sitting in somebody's history rail. Left
    # unflagged they would each be renamed by the agent on their next turn —
    # a sidebar quietly rewriting itself as old chats are picked back up. The
    # naming tool is for conversations from here on.
    op.execute(
        """
        UPDATE web_chat_conversations
        SET title_is_custom = true
        WHERE title IS NOT NULL AND title <> 'New Chat'
        """
    )


def downgrade() -> None:
    op.drop_column("web_chat_conversations", "archived_at")
    op.drop_column("web_chat_conversations", "title_is_custom")
