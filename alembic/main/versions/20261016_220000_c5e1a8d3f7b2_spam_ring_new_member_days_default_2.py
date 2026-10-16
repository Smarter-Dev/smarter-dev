"""Spam ring: new guilds count a member as new for 2 days, not 7.

Every ring account seen so far posted within minutes of joining, so the
detector only needs to look at members who joined in the last 2 days. This
revision changes only the server default of
``moderation_filter_configs.spam_ring_new_member_days``. Existing rows keep
the value they hold; the production guild's row was set to 2 by hand (#170).

Revision ID: c5e1a8d3f7b2
Revises: f2b7d4e9c1a6
Create Date: 2026-10-16 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5e1a8d3f7b2"
down_revision: str | None = "f2b7d4e9c1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "moderation_filter_configs",
        "spam_ring_new_member_days",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("2"),
    )


def downgrade() -> None:
    op.alter_column(
        "moderation_filter_configs",
        "spam_ring_new_member_days",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("7"),
    )
