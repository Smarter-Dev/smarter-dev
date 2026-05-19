"""add chat agent conversations

Creates three tables in the `skrift` schema for the operator dashboard
that surfaces Discord chat-agent activity:

- `chat_agent_engagements` — one row per ``ChannelEngine`` lifecycle
- `chat_agent_turns` — one row per agent fire (SendResponse or NoResponse)
- `chat_agent_compaction_events` — one row per part the history compactor
  summarised, during a turn

Revision ID: a1b2c3d4e5f6
Revises: bef52194dfc9
Create Date: 2026-05-17 19:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "bef52194dfc9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_agent_engagements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("guild_name", sa.String(length=120), nullable=True),
        sa.Column("channel_name", sa.String(length=120), nullable=True),
        sa.Column("activation_user_id", sa.String(), nullable=False),
        sa.Column("activation_username", sa.String(length=100), nullable=False),
        sa.Column("activation_message_id", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivation_reason", sa.String(length=40), nullable=True),
        sa.Column("last_topic", sa.Text(), nullable=True),
        sa.Column("last_notes", sa.Text(), nullable=True),
        sa.Column(
            "total_chat_tokens_input",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_chat_tokens_output",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_compaction_tokens_input",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_compaction_tokens_output",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_voice_tokens_input",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_voice_tokens_output",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_agent_engagements")),
    )
    op.create_index(
        op.f("ix_chat_agent_engagements_guild_id"),
        "chat_agent_engagements",
        ["guild_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_engagements_channel_id"),
        "chat_agent_engagements",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_engagements_activation_user_id"),
        "chat_agent_engagements",
        ["activation_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_engagements_started_at"),
        "chat_agent_engagements",
        ["started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_engagements_ended_at"),
        "chat_agent_engagements",
        ["ended_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_agent_engagements_guild_started",
        "chat_agent_engagements",
        ["guild_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_agent_engagements_channel_started",
        "chat_agent_engagements",
        ["channel_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_agent_engagements_user_started",
        "chat_agent_engagements",
        ["activation_user_id", "started_at"],
        unique=False,
    )

    op.create_table(
        "chat_agent_turns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("engagement_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.String(length=16), nullable=False),
        sa.Column("turn_kind", sa.String(length=16), nullable=False),
        sa.Column("output_kind", sa.String(length=16), nullable=False),
        sa.Column("triggering_messages", sa.JSON(), nullable=False),
        sa.Column("agent_output", sa.JSON(), nullable=False),
        sa.Column("model_messages_delta", sa.JSON(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "chat_tokens_input",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "chat_tokens_output",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("chat_model_name", sa.String(length=80), nullable=True),
        sa.Column(
            "chat_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "voice_tokens_input",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "voice_tokens_output",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("voice_model_name", sa.String(length=80), nullable=True),
        sa.Column(
            "voice_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("voice_sent_ok", sa.Boolean(), nullable=True),
        sa.Column("voice_send_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["chat_agent_engagements.id"],
            name=op.f("fk_chat_agent_turns_engagement_id_chat_agent_engagements"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_agent_turns")),
    )
    op.create_index(
        op.f("ix_chat_agent_turns_engagement_id"),
        "chat_agent_turns",
        ["engagement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chat_agent_turns_started_at"),
        "chat_agent_turns",
        ["started_at"],
        unique=False,
    )

    op.create_table(
        "chat_agent_compaction_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("event_kind", sa.String(length=24), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=True),
        sa.Column("original_content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("original_chars", sa.Integer(), nullable=False),
        sa.Column("summary_chars", sa.Integer(), nullable=False),
        sa.Column("chars_saved", sa.Integer(), nullable=False),
        sa.Column(
            "summarizer_tokens_input",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "summarizer_tokens_output",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("summarizer_model_name", sa.String(length=80), nullable=True),
        sa.Column(
            "summarizer_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
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
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["chat_agent_turns.id"],
            name=op.f("fk_chat_agent_compaction_events_turn_id_chat_agent_turns"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_chat_agent_compaction_events")
        ),
    )
    op.create_index(
        op.f("ix_chat_agent_compaction_events_turn_id"),
        "chat_agent_compaction_events",
        ["turn_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_chat_agent_compaction_events_turn_id"),
        table_name="chat_agent_compaction_events",
    )
    op.drop_table("chat_agent_compaction_events")
    op.drop_index(
        op.f("ix_chat_agent_turns_started_at"), table_name="chat_agent_turns"
    )
    op.drop_index(
        op.f("ix_chat_agent_turns_engagement_id"),
        table_name="chat_agent_turns",
    )
    op.drop_table("chat_agent_turns")
    op.drop_index(
        "ix_chat_agent_engagements_user_started",
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        "ix_chat_agent_engagements_channel_started",
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        "ix_chat_agent_engagements_guild_started",
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        op.f("ix_chat_agent_engagements_ended_at"),
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        op.f("ix_chat_agent_engagements_started_at"),
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        op.f("ix_chat_agent_engagements_activation_user_id"),
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        op.f("ix_chat_agent_engagements_channel_id"),
        table_name="chat_agent_engagements",
    )
    op.drop_index(
        op.f("ix_chat_agent_engagements_guild_id"),
        table_name="chat_agent_engagements",
    )
    op.drop_table("chat_agent_engagements")
