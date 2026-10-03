"""Chat bot purge requests and the purged-user block list

A purge removes one Discord user from the chat bot's memory and history; the
block list keeps both runtimes from reading that user's old messages again.
All three tables are new, so the build before this one never reads them.

Revision ID: f3a8b1c6d2e9
Revises: e7b2c4d9a1f3
Create Date: 2026-10-03 23:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3a8b1c6d2e9"
down_revision: str | None = "e7b2c4d9a1f3"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
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
        "chat_bot_blocked_users",
        sa.Column("discord_user_id", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint(
            "discord_user_id", name=op.f("pk_chat_bot_blocked_users")
        ),
    )
    op.create_table(
        "chat_bot_blocked_users_revision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_bot_blocked_users_revision")),
    )
    op.create_table(
        "chat_bot_purge_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("discord_user_id", sa.String(20), nullable=True),
        sa.Column("names", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("check_report", sa.JSON(), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_bot_purge_requests")),
    )
    op.create_index(
        op.f("ix_chat_bot_purge_requests_discord_user_id"),
        "chat_bot_purge_requests",
        ["discord_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_chat_bot_purge_requests_discord_user_id"),
        table_name="chat_bot_purge_requests",
    )
    op.drop_table("chat_bot_purge_requests")
    op.drop_table("chat_bot_blocked_users_revision")
    op.drop_table("chat_bot_blocked_users")
