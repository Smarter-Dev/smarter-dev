"""Widen admin_handlers.trigger_type to varchar(32).

The longest admin trigger, ``member_rules_accepted``, is 21 chars, but the
column was varchar(20) — every insert of a handler on that trigger (e.g. the
``onboard-and-promote`` extension) raised StringDataRightTruncationError. The
value was never storable before, so widening cannot truncate existing data.
``channel_handlers.trigger_type`` stays varchar(20): its check constraint only
admits message/reaction/schedule/timer (all <= 8 chars).

Revision ID: d1e3f5a7c9b2
Revises: 7aa20a55c255
Create Date: 2026-07-21 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e3f5a7c9b2"
down_revision: Union[str, None] = "7aa20a55c255"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "admin_handlers",
        "trigger_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "admin_handlers",
        "trigger_type",
        existing_type=sa.String(length=32),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
