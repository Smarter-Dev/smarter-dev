"""Give guild memory a public block and a public-page switch

The nightly dream writes a fourth block, ``public_content``: the memory with
every person removed, shown on the public ``/chat-agent`` page (#103). It has
the memory's own cap. ``public_page_enabled`` is the per-guild switch for that
page, off on every row.

Additive only, so the build before this one keeps working against it: it
never selects the new columns, and its upsert leaves them as they are.

Revision ID: b4e8a1d6c2f9
Revises: e7b3c5a9d2f1
Create Date: 2026-10-08 15:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b4e8a1d6c2f9"
down_revision: str | None = "e7b3c5a9d2f1"
branch_labels = None
depends_on = None

_TABLE = "chat_agent_guild_memory"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("public_content", sa.String(2000), nullable=False, server_default=""),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "public_page_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "public_page_enabled")
    op.drop_column(_TABLE, "public_content")
