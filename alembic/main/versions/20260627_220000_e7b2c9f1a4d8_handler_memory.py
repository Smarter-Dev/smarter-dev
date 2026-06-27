"""Add persistent per-handler memory (JSON key/value store).

Gives both channel_handlers and admin_handlers a `memory` JSON column: a small
key/value store the script reads/writes via the memory_* functions, persisted
across fires (counters, seen-sets, cooldown timestamps). Defaults to {} so
existing rows are unaffected.

Revision ID: e7b2c9f1a4d8
Revises: d4f1a2b3c6e7
Create Date: 2026-06-27 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b2c9f1a4d8"
down_revision: Union[str, None] = "d4f1a2b3c6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("channel_handlers", "admin_handlers"):
        op.add_column(
            table,
            sa.Column(
                "memory",
                sa.JSON(),
                server_default="{}",
                nullable=False,
            ),
        )


def downgrade() -> None:
    for table in ("channel_handlers", "admin_handlers"):
        op.drop_column(table, "memory")
