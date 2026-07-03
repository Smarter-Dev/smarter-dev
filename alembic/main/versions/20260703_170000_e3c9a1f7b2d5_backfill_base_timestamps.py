"""Backfill Base timestamp columns on tables whose migrations omitted them.

``Base`` declares ``created_at`` / ``updated_at`` (server_default now()) on every
model, so SQLAlchemy references them on insert (RETURNING) and select. Several
tables were created by migrations that never added these columns, which only
surfaced once those code paths ran against a populated prod DB:

- ``member_activity``          — missing created_at + updated_at
- ``sudo_membership_reminders`` — missing created_at + updated_at
- ``moderation_actions``        — missing updated_at (had created_at)

Existing rows backfill to now() via the server default.

Revision ID: e3c9a1f7b2d5
Revises: d8b2f1e6a3c9
Create Date: 2026-07-03 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c9a1f7b2d5"
down_revision: Union[str, None] = "d8b2f1e6a3c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts_column(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def upgrade() -> None:
    op.add_column("member_activity", _ts_column("created_at"))
    op.add_column("member_activity", _ts_column("updated_at"))
    op.add_column("sudo_membership_reminders", _ts_column("created_at"))
    op.add_column("sudo_membership_reminders", _ts_column("updated_at"))
    op.add_column("moderation_actions", _ts_column("updated_at"))


def downgrade() -> None:
    op.drop_column("moderation_actions", "updated_at")
    op.drop_column("sudo_membership_reminders", "updated_at")
    op.drop_column("sudo_membership_reminders", "created_at")
    op.drop_column("member_activity", "updated_at")
    op.drop_column("member_activity", "created_at")
