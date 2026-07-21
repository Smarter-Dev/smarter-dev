"""Admin-tier dm_message trigger.

Extends ``ck_admin_handlers_trigger_type`` to admit the inbound-DM trigger
(``dm_message``) — the tenth admin trigger family, from
docs/v2/feature-parity/staff-communication-channels.md E1.
``ck_channel_handlers_trigger_type`` is deliberately UNCHANGED: a member-authored
channel handler must never see other users' DMs.

Revision ID: e5f7a9c1b3d2
Revises: d4f6a8b0c2e1
Create Date: 2026-07-18 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e5f7a9c1b3d2"
down_revision: Union[str, None] = "d4f6a8b0c2e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADMIN_TRIGGER_TYPES_WITH_DM = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create', 'dm_message')"
)
_ADMIN_TRIGGER_TYPES_WITHOUT_DM = (
    "trigger_type IN ('message', 'reaction', 'schedule', 'timer', "
    "'member_join', 'member_leave', 'member_rules_accepted', "
    "'member_role_change', 'thread_create')"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_WITH_DM,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_admin_handlers_trigger_type"), "admin_handlers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_admin_handlers_trigger_type"),
        "admin_handlers",
        _ADMIN_TRIGGER_TYPES_WITHOUT_DM,
    )
