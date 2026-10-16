"""Add the spam ring detector's settings and its incident table.

Six columns on ``moderation_filter_configs`` configure the detector in
``smarter_dev/bot/spam_ring.py``. Unlike the content filters and the spam
engine it ships ON: ``spam_ring_enabled`` has ``server_default true``, so any
existing row turns it on, and a guild with no row runs it on the model
defaults. Every other setting carries its default as a server default too.

``spam_ring_incidents`` holds the transcript the detector saves before it
deletes anything. It is member content, so the hourly retention sweep deletes
each row 90 days after it was written (``MODERATION_TRANSCRIPT_MAX``).

Revision ID: f2b7d4e9c1a6
Revises: a3f6c9e2d4b8
Create Date: 2026-10-16 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2b7d4e9c1a6"
down_revision: str | None = "a3f6c9e2d4b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "moderation_filter_configs",
        sa.Column(
            "spam_ring_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "moderation_filter_configs",
        sa.Column(
            "spam_ring_new_member_days",
            sa.Integer(),
            server_default=sa.text("7"),
            nullable=False,
        ),
    )
    op.add_column(
        "moderation_filter_configs",
        sa.Column(
            "spam_ring_timeout_seconds",
            sa.Integer(),
            server_default=sa.text("604800"),
            nullable=False,
        ),
    )
    op.add_column(
        "moderation_filter_configs",
        sa.Column(
            "spam_ring_lookback_minutes",
            sa.Integer(),
            server_default=sa.text("60"),
            nullable=False,
        ),
    )
    op.add_column(
        "moderation_filter_configs",
        sa.Column(
            "spam_ring_followup_minutes",
            sa.Integer(),
            server_default=sa.text("15"),
            nullable=False,
        ),
    )
    op.add_column(
        "moderation_filter_configs",
        sa.Column("spam_ring_report_channel_id", sa.String(), nullable=True),
    )

    op.create_table(
        "spam_ring_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("trigger_message_id", sa.String(), nullable=False),
        sa.Column("participant_user_ids", sa.JSON(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("report_channel_id", sa.String(), nullable=True),
        sa.Column("report_message_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_spam_ring_incidents_created_at", "spam_ring_incidents", ["created_at"]
    )
    op.create_index(
        "ix_spam_ring_incidents_guild_channel",
        "spam_ring_incidents",
        ["guild_id", "channel_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spam_ring_incidents_guild_channel", table_name="spam_ring_incidents"
    )
    op.drop_index("ix_spam_ring_incidents_created_at", table_name="spam_ring_incidents")
    op.drop_table("spam_ring_incidents")
    for column in (
        "spam_ring_report_channel_id",
        "spam_ring_followup_minutes",
        "spam_ring_lookback_minutes",
        "spam_ring_timeout_seconds",
        "spam_ring_new_member_days",
        "spam_ring_enabled",
    ):
        op.drop_column("moderation_filter_configs", column)
