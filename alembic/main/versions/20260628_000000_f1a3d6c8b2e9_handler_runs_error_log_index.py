"""Index handler_runs by (handler_id, fired_at) for the per-handler error log.

The admin page queries each handler's recent non-ok runs ordered by fired_at;
a composite index over (handler_id, fired_at) serves that filter+order directly.

Revision ID: f1a3d6c8b2e9
Revises: e7b2c9f1a4d8
Create Date: 2026-06-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f1a3d6c8b2e9"
down_revision: Union[str, None] = "e7b2c9f1a4d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_handler_runs_handler_id_fired_at",
        "handler_runs",
        ["handler_id", "fired_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_handler_runs_handler_id_fired_at", table_name="handler_runs")
