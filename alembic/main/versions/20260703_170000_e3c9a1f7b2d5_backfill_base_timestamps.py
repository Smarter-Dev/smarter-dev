"""Backfill Base timestamp columns on tables whose migrations omitted them.

``Base`` declares ``created_at`` / ``updated_at`` (server_default now()) on every
model, so SQLAlchemy references them on insert (RETURNING) and select. Several
tables were created by migrations that never added these columns, which only
surfaced once those code paths ran against a populated prod DB:

- ``member_activity``          — missing created_at + updated_at
- ``sudo_membership_reminders`` — missing created_at + updated_at
- ``moderation_actions``        — missing updated_at (had created_at)

Existing rows backfill to now() via the server default.

On a FRESH database the earlier migrations already create these columns
(they were regenerated from the models), so each add is guarded by an
information_schema existence check — prod (already applied) is unaffected.

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


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "AND column_name = :column"
            ),
            {"schema": schema, "table": table_name, "column": column_name},
        ).scalar()
    )


def _add_ts_column_if_missing(table_name: str, column_name: str) -> None:
    if not _column_exists(table_name, column_name):
        op.add_column(table_name, _ts_column(column_name))


def upgrade() -> None:
    _add_ts_column_if_missing("member_activity", "created_at")
    _add_ts_column_if_missing("member_activity", "updated_at")
    _add_ts_column_if_missing("sudo_membership_reminders", "created_at")
    _add_ts_column_if_missing("sudo_membership_reminders", "updated_at")
    _add_ts_column_if_missing("moderation_actions", "updated_at")


def downgrade() -> None:
    op.drop_column("moderation_actions", "updated_at")
    op.drop_column("sudo_membership_reminders", "updated_at")
    op.drop_column("sudo_membership_reminders", "created_at")
    op.drop_column("member_activity", "updated_at")
    op.drop_column("member_activity", "created_at")
