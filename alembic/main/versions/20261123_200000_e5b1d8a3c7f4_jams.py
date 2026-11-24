"""jams, their shared Discord setup and their scheduled messages

#497 (2026-11-23). ``jam_discord_setups`` holds, per guild, the ids of the
Discord category, channels and role every jam in that guild shares. ``jams``
holds one row per code jam, with the ids of its two showcase forum tags.
``jam_messages`` holds one-off messages scheduled into the jam channels; the
bot claims each one before posting it.

All three tables are new, so the previous build never reads them and a
rollback only has to drop them.

Revision ID: e5b1d8a3c7f4
Revises: c8e2a5f7b1d3
Create Date: 2026-11-23 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5b1d8a3c7f4"
down_revision: str | None = "c8e2a5f7b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    # Base gives every model these two.
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "jam_discord_setups",
        sa.Column("guild_id", sa.String(length=22), nullable=False),
        sa.Column("category_id", sa.String(length=22), nullable=True),
        sa.Column("news_channel_id", sa.String(length=22), nullable=True),
        sa.Column("chat_channel_id", sa.String(length=22), nullable=True),
        sa.Column("showcase_channel_id", sa.String(length=22), nullable=True),
        sa.Column("role_id", sa.String(length=22), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_jam_discord_setups")),
    )
    op.create_table(
        "jams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(length=22), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("tag_name", sa.String(length=20), nullable=False),
        sa.Column("max_team_size", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submissions_close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("voting_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winners_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("showcase_tag_id", sa.String(length=22), nullable=True),
        sa.Column("year_tag_id", sa.String(length=22), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jams")),
        sa.CheckConstraint(
            "max_team_size >= 1", name=op.f("ck_jams_max_team_size_positive")
        ),
        sa.CheckConstraint(
            "submissions_close_at > starts_at",
            name=op.f("ck_jams_closes_after_start"),
        ),
    )
    op.create_index("ix_jams_guild_id_starts_at", "jams", ["guild_id", "starts_at"])
    op.create_table(
        "jam_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jam_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_key", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("mention_role", sa.Boolean(), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jam_messages")),
        sa.ForeignKeyConstraint(
            ["jam_id"],
            ["jams.id"],
            name=op.f("fk_jam_messages_jam_id_jams"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')",
            name=op.f("ck_jam_messages_status"),
        ),
        sa.CheckConstraint(
            "channel_key IN ('news', 'chat')",
            name=op.f("ck_jam_messages_channel_key"),
        ),
    )
    op.create_index("ix_jam_messages_jam_id", "jam_messages", ["jam_id"])
    op.create_index("ix_jam_messages_due", "jam_messages", ["status", "send_at"])


def downgrade() -> None:
    op.drop_table("jam_messages")
    op.drop_table("jams")
    op.drop_table("jam_discord_setups")
