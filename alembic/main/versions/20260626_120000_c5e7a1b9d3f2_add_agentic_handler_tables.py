"""Add agentic handler system tables.

Three tables:
- ``channel_handlers`` — member-created sandboxed automations. A partial unique
  index makes message/reaction triggers single-listener per channel while
  schedule/timer triggers coexist.
- ``handler_runs`` — durable per-fire audit incl. budget spend.
- ``privileged_routines`` — the separate admin-only moderation tier.

Revision ID: c5e7a1b9d3f2
Revises: a1c2e3f4b5d6
Create Date: 2026-06-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5e7a1b9d3f2"
down_revision: Union[str, None] = "a1c2e3f4b5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "channel_handlers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("channel_id", sa.String(length=20), nullable=False),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("settings", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("scheduled_job_id", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "trigger_type IN ('message', 'reaction', 'schedule', 'timer')",
            name=op.f("ck_channel_handlers_trigger_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_handlers")),
    )
    op.create_index(
        "ix_channel_handlers_channel_id", "channel_handlers", ["channel_id"]
    )
    op.create_index(
        "uq_channel_handlers_event_listener",
        "channel_handlers",
        ["channel_id", "trigger_type"],
        unique=True,
        postgresql_where=sa.text("trigger_type IN ('message', 'reaction')"),
    )

    op.create_table(
        "handler_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("handler_id", sa.UUID(), nullable=False),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_context", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("cap", sa.String(length=40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("messages_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("web_searches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("web_reads", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
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
        sa.CheckConstraint(
            "outcome IN ('ok', 'cap_exceeded', 'error', 'rejected')",
            name=op.f("ck_handler_runs_outcome"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handler_runs")),
    )
    op.create_index(
        "ix_handler_runs_handler_id", "handler_runs", ["handler_id"]
    )

    op.create_table(
        "privileged_routines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("channel_id", sa.String(length=20), nullable=True),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("settings", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("action", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_by_admin", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("scheduled_job_id", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "trigger_type IN ('message', 'reaction', 'schedule', 'timer')",
            name=op.f("ck_privileged_routines_trigger_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_privileged_routines")),
    )
    op.create_index(
        "ix_privileged_routines_channel_id", "privileged_routines", ["channel_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_privileged_routines_channel_id", table_name="privileged_routines"
    )
    op.drop_table("privileged_routines")
    op.drop_index("ix_handler_runs_handler_id", table_name="handler_runs")
    op.drop_table("handler_runs")
    op.drop_index(
        "uq_channel_handlers_event_listener", table_name="channel_handlers"
    )
    op.drop_index(
        "ix_channel_handlers_channel_id", table_name="channel_handlers"
    )
    op.drop_table("channel_handlers")
