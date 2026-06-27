"""Repurpose privileged_routines into admin_handlers (script-based admin tier).

Renames privileged_routines -> admin_handlers, drops the structured `action`
and single `channel_id`, adds `description`/`script`/`channel_ids` (scope; []=all
channels). Adds `handler_kind` + `mod_actions` to handler_runs so one audit table
covers both the standard and admin tiers. The table is empty in prod, so the
column changes are safe.

Revision ID: d4f1a2b3c6e7
Revises: c5e7a1b9d3f2
Create Date: 2026-06-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f1a2b3c6e7"
down_revision: Union[str, None] = "c5e7a1b9d3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # handler_runs: discriminator + moderation counter
    op.add_column(
        "handler_runs",
        sa.Column(
            "handler_kind",
            sa.String(length=20),
            server_default="standard",
            nullable=False,
        ),
    )
    op.add_column(
        "handler_runs",
        sa.Column("mod_actions", sa.Integer(), server_default="0", nullable=False),
    )

    # privileged_routines -> admin_handlers (script-based)
    op.rename_table("privileged_routines", "admin_handlers")
    op.drop_constraint(
        op.f("ck_privileged_routines_trigger_type"), "admin_handlers", type_="check"
    )
    op.drop_index("ix_privileged_routines_channel_id", table_name="admin_handlers")
    op.drop_column("admin_handlers", "action")
    op.drop_column("admin_handlers", "channel_id")
    op.add_column(
        "admin_handlers",
        sa.Column("description", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "admin_handlers",
        sa.Column("script", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "admin_handlers",
        sa.Column("channel_ids", sa.JSON(), server_default="[]", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        "trigger_type IN ('message', 'reaction', 'schedule', 'timer')",
    )
    op.create_index("ix_admin_handlers_guild_id", "admin_handlers", ["guild_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_handlers_guild_id", table_name="admin_handlers")
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.drop_column("admin_handlers", "channel_ids")
    op.drop_column("admin_handlers", "script")
    op.drop_column("admin_handlers", "description")
    op.add_column(
        "admin_handlers",
        sa.Column("channel_id", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "admin_handlers",
        sa.Column("action", sa.JSON(), server_default="{}", nullable=False),
    )
    op.create_index(
        "ix_privileged_routines_channel_id", "admin_handlers", ["channel_id"]
    )
    op.create_check_constraint(
        op.f("ck_privileged_routines_trigger_type"),
        "admin_handlers",
        "trigger_type IN ('message', 'reaction', 'schedule', 'timer')",
    )
    op.rename_table("admin_handlers", "privileged_routines")
    op.drop_column("handler_runs", "mod_actions")
    op.drop_column("handler_runs", "handler_kind")
