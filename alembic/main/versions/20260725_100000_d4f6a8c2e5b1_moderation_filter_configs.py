"""Add the per-guild moderation filter + spam engine configuration table.

Creates ``moderation_filter_configs``: one row per guild holding the content
filter settings (blocked TLDs, invite filter, webhook killer) and the spam
engine thresholds. Both subsystems ship disabled; every threshold defaults to
the legacy bot's value so enabling a subsystem reproduces historical behaviour.

Revision ID: d4f6a8c2e5b1
Revises: c9e3f5b7d2a0
Create Date: 2026-07-25 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4f6a8c2e5b1"
down_revision: str | None = "c9e3f5b7d2a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moderation_filter_configs",
        sa.Column("guild_id", sa.String(), nullable=False),
        # Content filters
        sa.Column(
            "content_filter_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "blocked_tlds",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "invite_filter_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "invite_filter_exempt_category_ids",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "staff_exempt_role_ids",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "webhook_killer_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("mod_log_channel_id", sa.String(), nullable=True),
        # Spam engine
        sa.Column(
            "spam_engine_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "message_rate_threshold",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "message_rate_window_seconds",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "channel_spread_threshold",
            sa.Integer(),
            server_default=sa.text("3"),
            nullable=False,
        ),
        sa.Column(
            "channel_spread_window_seconds",
            sa.Integer(),
            server_default=sa.text("15"),
            nullable=False,
        ),
        sa.Column(
            "duplicate_message_min_length",
            sa.Integer(),
            server_default=sa.text("15"),
            nullable=False,
        ),
        sa.Column(
            "duplicate_message_window_seconds",
            sa.Integer(),
            server_default=sa.text("60"),
            nullable=False,
        ),
        sa.Column(
            "mass_mention_window_seconds",
            sa.Integer(),
            server_default=sa.text("15"),
            nullable=False,
        ),
        sa.Column(
            "warning_reoffense_window_seconds",
            sa.Integer(),
            server_default=sa.text("120"),
            nullable=False,
        ),
        sa.Column(
            "mute_duration_seconds",
            sa.Integer(),
            server_default=sa.text("86400"),
            nullable=False,
        ),
        sa.Column("mod_alert_channel_id", sa.String(), nullable=True),
        sa.Column("mod_ping_role_id", sa.String(), nullable=True),
        sa.Column("scam_log_channel_id", sa.String(), nullable=True),
        # Timestamps
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
        sa.PrimaryKeyConstraint(
            "guild_id", name=op.f("pk_moderation_filter_configs")
        ),
    )
    op.create_index(
        "ix_moderation_filter_configs_guild_id",
        "moderation_filter_configs",
        ["guild_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_moderation_filter_configs_guild_id",
        table_name="moderation_filter_configs",
    )
    op.drop_table("moderation_filter_configs")
