"""add per-channel auto-respond and response filtering settings

Adds three per-channel settings to ``channel_model_overrides``:

- ``auto_respond`` — when true the chat bot activates on ANY channel message,
  not just @mentions (NOT NULL, defaults false).
- ``fallback_model_key`` — a catalog key used when the primary model is
  unavailable, or NULL.
- ``response_filter`` — free-text instructions describing which messages
  deserve a response, or NULL.

Revision ID: f1a2b3c4d5e6
Revises: cfcaa2cbf2b0
Create Date: 2026-07-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "b2e4f7a1c3d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channel_model_overrides",
        sa.Column(
            "auto_respond",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "channel_model_overrides",
        sa.Column("fallback_model_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "channel_model_overrides",
        sa.Column("response_filter", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_model_overrides", "response_filter")
    op.drop_column("channel_model_overrides", "fallback_model_key")
    op.drop_column("channel_model_overrides", "auto_respond")
