"""Add handler_runs.role_changes counter.

Role grants/revokes (add_role/remove_role) are metered by a per-fire counter
separate from mod_actions; this column records the spend on every handler run
(standard-tier rows always record 0, since only admin handlers can mutate roles).

Revision ID: a7c2e9f4b1d8
Revises: c3d5e7f9a1b2
Create Date: 2026-07-18 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c2e9f4b1d8"
down_revision: Union[str, None] = "c3d5e7f9a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "handler_runs",
        sa.Column("role_changes", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("handler_runs", "role_changes")
