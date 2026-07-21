"""Admin-tier mod_action trigger + handler_runs.lookups counter.

Extends ``ck_admin_handlers_trigger_type`` to admit the synthetic ``mod_action``
trigger — fired (bot-side) whenever a ``ModerationAction`` row is written, so a
mod-log-formatter handler can own audit-channel formatting for manual, AI, and
handler actions alike (docs/v2/feature-parity/automated-and-command-moderation.md
§3.5). Adds a nullable-default ``lookups`` counter to ``handler_runs`` for the new
metered mod-audit reads (list_mod_actions / get_member_info / search_guild_members,
§3.7). ``ck_channel_handlers_trigger_type`` is deliberately UNCHANGED: the standard
tier's vocabulary does not grow.

Revision ID: b7d9f1a3c5e2
Revises: a1b3c5d7e9f0
Create Date: 2026-07-18 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7d9f1a3c5e2"
down_revision: Union[str, None] = "a1b3c5d7e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADMIN_TRIGGER_TYPES_WITH_MOD_ACTION = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create', 'dm_message', 'message_edit', "
    "'mod_action')"
)
_ADMIN_TRIGGER_TYPES_WITHOUT_MOD_ACTION = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create', 'dm_message', 'message_edit')"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_WITH_MOD_ACTION,
    )
    op.add_column(
        "handler_runs",
        sa.Column(
            "lookups",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("handler_runs", "lookups")
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_WITHOUT_MOD_ACTION,
    )
