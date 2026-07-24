"""add per-channel writer_model for two-stage chat mode

Adds a nullable ``writer_model`` column to ``channel_model_overrides``. When
set/non-empty the channel runs the two-stage worker+writer chat mode
(``model_key`` names the WORKER, ``writer_model`` the WRITER); NULL keeps the
existing single-agent behaviour.

Revision ID: b8d2f4a6c1e9
Revises: a6c8e1f3b5d7
Create Date: 2026-07-24 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d2f4a6c1e9"
down_revision: Union[str, None] = "a6c8e1f3b5d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channel_model_overrides",
        sa.Column("writer_model", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_model_overrides", "writer_model")
