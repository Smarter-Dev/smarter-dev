"""Admin-tier message_edit trigger.

Extends ``ck_admin_handlers_trigger_type`` to admit the human-message-edit
trigger (``message_edit``) — a channel-keyed admin auto-mod trigger dispatched
from ``hikari.GuildMessageUpdateEvent``
(docs/v2/feature-parity/automated-and-command-moderation.md §3.3).
``ck_channel_handlers_trigger_type`` is deliberately UNCHANGED: the standard
tier's vocabulary does not grow. ``handler_runs`` needs no new counter.

Revision ID: a1b3c5d7e9f0
Revises: e5f7a9c1b3d2
Create Date: 2026-07-18 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a1b3c5d7e9f0"
down_revision: Union[str, None] = "e5f7a9c1b3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADMIN_TRIGGER_TYPES_WITH_EDIT = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create', 'dm_message', 'message_edit')"
)
_ADMIN_TRIGGER_TYPES_WITHOUT_EDIT = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create', 'dm_message')"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_WITH_EDIT,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_WITHOUT_EDIT,
    )
