"""Add the protected Discord chat-agent error log.

Revision ID: a6c8e1f3b5d7
Revises: d1e3f5a7c9b2
Create Date: 2026-07-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a6c8e1f3b5d7"
down_revision: str | None = "d1e3f5a7c9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_agent_errors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engagement_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(length=16), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("reasoning_level", sa.String(length=16), nullable=True),
        sa.Column("error_type", sa.String(length=255), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=False),
        sa.Column("provider_status_code", sa.Integer(), nullable=True),
        sa.Column("provider_body", sa.Text(), nullable=True),
        sa.Column("error_context", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["chat_agent_engagements.id"],
            name=op.f(
                "fk_chat_agent_errors_engagement_id_chat_agent_engagements"
            ),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_agent_errors")),
    )
    op.create_index(
        op.f("ix_chat_agent_errors_engagement_id"),
        "chat_agent_errors",
        ["engagement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_errors_request_id"),
        "chat_agent_errors",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_errors_guild_id"),
        "chat_agent_errors",
        ["guild_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_errors_channel_id"),
        "chat_agent_errors",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_errors_model_name"),
        "chat_agent_errors",
        ["model_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_errors_occurred_at"),
        "chat_agent_errors",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_agent_errors_guild_occurred",
        "chat_agent_errors",
        ["guild_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_agent_errors_model_occurred",
        "chat_agent_errors",
        ["model_name", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_agent_errors_model_occurred", table_name="chat_agent_errors"
    )
    op.drop_index(
        "ix_chat_agent_errors_guild_occurred", table_name="chat_agent_errors"
    )
    op.drop_index(
        op.f("ix_chat_agent_errors_occurred_at"),
        table_name="chat_agent_errors",
    )
    op.drop_index(
        op.f("ix_chat_agent_errors_model_name"),
        table_name="chat_agent_errors",
    )
    op.drop_index(
        op.f("ix_chat_agent_errors_channel_id"),
        table_name="chat_agent_errors",
    )
    op.drop_index(
        op.f("ix_chat_agent_errors_guild_id"), table_name="chat_agent_errors"
    )
    op.drop_index(
        op.f("ix_chat_agent_errors_request_id"), table_name="chat_agent_errors"
    )
    op.drop_index(
        op.f("ix_chat_agent_errors_engagement_id"),
        table_name="chat_agent_errors",
    )
    op.drop_table("chat_agent_errors")
