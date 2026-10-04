"""Chat bot purge requests and the purged-user block list

A purge removes one Discord user from the chat bot's memory and history; the
block list keeps both runtimes from reading that user's old messages again.
All three tables are new, so the build before this one never reads them.

Revision ID: f3a8b1c6d2e9
Revises: c5e1a9d3b7f2
Create Date: 2026-10-03 23:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3a8b1c6d2e9"
down_revision: str | None = "c5e1a9d3b7f2"
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
        sa.Column("discord_user_id", sa.String(22), nullable=False),
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
        sa.Column("discord_user_id", sa.String(22), nullable=True),
        sa.Column("names", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("check_report", sa.JSON(), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_bot_purge_requests")),
    )
    # One open request per user; closed requests keep no user ID at all.
    op.create_index(
        "uq_chat_bot_purge_requests_open_user",
        "chat_bot_purge_requests",
        ["discord_user_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )


def downgrade() -> None:
    # Dropping the block list would let both runtimes read everyone who was
    # purged straight back into history. Refuse while anyone is on it.
    blocked = op.get_bind().execute(
        sa.text("SELECT count(*) FROM chat_bot_blocked_users")
    ).scalar()
    if blocked:
        raise RuntimeError(
            f"Refusing to downgrade: chat_bot_blocked_users has {blocked} row(s). "
            "Dropping it un-blocks every purged user. Move the list elsewhere first."
        )
    op.drop_index(
        "uq_chat_bot_purge_requests_open_user",
        table_name="chat_bot_purge_requests",
    )
    op.drop_table("chat_bot_purge_requests")
    op.drop_table("chat_bot_blocked_users_revision")
    op.drop_table("chat_bot_blocked_users")
