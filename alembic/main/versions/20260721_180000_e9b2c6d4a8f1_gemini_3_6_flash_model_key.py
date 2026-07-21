"""remap gemini-3-5-flash channel overrides to gemini-3-6-flash

Gemini 3.6 Flash replaced 3.5 Flash in the model catalog (2026-07-21), so the
``gemini-3-5-flash`` catalog key no longer resolves. Any channel override
pinned to it (as the primary model or as the fallback) would silently fall
back to the server default; remap those rows to ``gemini-3-6-flash`` instead.

Historical usage rows keep their stored ``gemini-3.5-flash`` wire id — they
record what actually ran and are priced against that id.

Revision ID: e9b2c6d4a8f1
Revises: c4a1d9f2e6b8
Create Date: 2026-07-21 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9b2c6d4a8f1"
down_revision: Union[str, None] = "c4a1d9f2e6b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = "gemini-3-5-flash"
_NEW_KEY = "gemini-3-6-flash"


def _remap(from_key: str, to_key: str) -> None:
    op.execute(
        sa.text(
            "UPDATE channel_model_overrides SET model_key = :to_key "
            "WHERE model_key = :from_key"
        ).bindparams(from_key=from_key, to_key=to_key)
    )
    op.execute(
        sa.text(
            "UPDATE channel_model_overrides SET fallback_model_key = :to_key "
            "WHERE fallback_model_key = :from_key"
        ).bindparams(from_key=from_key, to_key=to_key)
    )


def upgrade() -> None:
    _remap(_OLD_KEY, _NEW_KEY)


def downgrade() -> None:
    _remap(_NEW_KEY, _OLD_KEY)
