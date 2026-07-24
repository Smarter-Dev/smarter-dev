"""rename channel override writer_model to drafter_model

Renames the ``writer_model`` column on ``channel_model_overrides`` to
``drafter_model`` to match the inverted two-stage semantics: the channel's
primary ``model_key`` is now the answering WRITER, and the optional
``drafter_model`` names the cheap context-gathering worker. The column rename
preserves existing values, which is intended — the previously-stored worker keys
now correctly name the drafter (the two live rows auto-correct).

Revision ID: c9e3f5b7d2a0
Revises: b8d2f4a6c1e9
Create Date: 2026-07-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c9e3f5b7d2a0"
down_revision: Union[str, None] = "b8d2f4a6c1e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "channel_model_overrides",
        "writer_model",
        new_column_name="drafter_model",
    )


def downgrade() -> None:
    op.alter_column(
        "channel_model_overrides",
        "drafter_model",
        new_column_name="writer_model",
    )
