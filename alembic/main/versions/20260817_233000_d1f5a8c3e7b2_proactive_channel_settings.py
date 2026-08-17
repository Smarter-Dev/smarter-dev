"""add proactive_channel_settings for the two-pass proactive bot

One row per channel: the admin /proactive on|off switch (default off) and
the agent-written watch_addendum that extends the watcher's wake criteria
across restarts.

Revision ID: d1f5a8c3e7b2
Revises: c9e4b7d2f6a3
Create Date: 2026-08-17 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1f5a8c3e7b2"
down_revision: Union[str, None] = "c9e4b7d2f6a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proactive_channel_settings",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("channel_id", sa.String(length=20), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "watch_addendum", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "channel_id", name="uq_proactive_channel_settings_channel_id"
        ),
    )
    op.create_index(
        "ix_proactive_channel_settings_guild_id",
        "proactive_channel_settings",
        ["guild_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proactive_channel_settings_guild_id",
        table_name="proactive_channel_settings",
    )
    op.drop_table("proactive_channel_settings")
