"""Drop the guild memory's public block

The public ``/chat-agent`` page shows the real blocks with every tagged person
masked (#104), so the dream no longer writes a separate public memory and
``public_content`` goes. ``public_page_enabled`` stays.

Ship only after the #104 code is live: that build no longer maps the column,
so no pod reads it when it goes. A pod that still maps it fails every read of
guild memory. Downgrade adds the column back, empty.

Revision ID: c7d2e9f4a1b6
Revises: b4e8a1d6c2f9
Create Date: 2026-10-08 18:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c7d2e9f4a1b6"
down_revision: str | None = "b4e8a1d6c2f9"
branch_labels = None
depends_on = None

_TABLE = "chat_agent_guild_memory"


def upgrade() -> None:
    op.drop_column(_TABLE, "public_content")


def downgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("public_content", sa.String(2000), nullable=False, server_default=""),
    )
