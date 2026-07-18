"""Add handler_runs.timers_scheduled counter.

One-shot timers armed via schedule_timer (E3) are metered by a per-fire counter;
this column records the spend on every handler run (both tiers can arm timers).

Revision ID: d4f6a8b0c2e1
Revises: a7c2e9f4b1d8
Create Date: 2026-07-18 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f6a8b0c2e1"
down_revision: Union[str, None] = "a7c2e9f4b1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "handler_runs",
        sa.Column(
            "timers_scheduled", sa.Integer(), server_default="0", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("handler_runs", "timers_scheduled")
