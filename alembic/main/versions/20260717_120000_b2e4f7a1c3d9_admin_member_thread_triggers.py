"""Admin-tier member/thread triggers + handler_runs read/thread-op counters.

Extends ``ck_admin_handlers_trigger_type`` to admit the five admin-only gateway
triggers (member_join/leave/rules_accepted/role_change, thread_create).
``ck_channel_handlers_trigger_type`` is deliberately UNCHANGED — the standard
tier's vocabulary does not grow. Adds ``handler_runs.discord_reads`` and
``handler_runs.thread_ops`` (metered list_threads reads and mutating thread ops),
matching the existing counter columns.

Revision ID: b2e4f7a1c3d9
Revises: cfcaa2cbf2b0
Create Date: 2026-07-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2e4f7a1c3d9"
down_revision: Union[str, None] = "cfcaa2cbf2b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADMIN_TRIGGER_TYPES_EXTENDED = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create')"
)
_ADMIN_TRIGGER_TYPES_ORIGINAL = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer')"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_EXTENDED,
    )
    op.add_column(
        "handler_runs",
        sa.Column("discord_reads", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "handler_runs",
        sa.Column("thread_ops", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("handler_runs", "thread_ops")
    op.drop_column("handler_runs", "discord_reads")
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_ORIGINAL,
    )
