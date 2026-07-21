"""allow channel model overrides without a pinned model

``channel_model_overrides.model_key`` becomes nullable: NULL means "keep the
server default model" while the row's budgets, auto-respond flag, fallback
model, and response filter still apply. Previously picking "Server default"
in ``/chat-bot-settings`` could only delete the whole row, so budgets etc.
could not be configured for a default-model channel.

Revision ID: f4a7d1c9e3b6
Revises: e9b2c6d4a8f1
Create Date: 2026-07-21 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a7d1c9e3b6"
down_revision: Union[str, None] = "e9b2c6d4a8f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "channel_model_overrides",
        "model_key",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    # Rows without a pinned model cannot survive a non-nullable column.
    op.execute("DELETE FROM channel_model_overrides WHERE model_key IS NULL")
    op.alter_column(
        "channel_model_overrides",
        "model_key",
        existing_type=sa.String(length=64),
        nullable=False,
    )
