"""admit Gemini 4 Argon to the chat catalog

Gemini 4 Argon joins the catalog on 2026-10-16 as a new key, ``gemini-4-argon``,
served by Google directly. It replaces nothing and no stored selection moves
onto it; people pick it themselves.

This revision only adds its ``chat_catalog_models`` row, enabled so it is
selectable as soon as the new build serves it. ``ensure_settings`` would
otherwise seed it disabled, leaving it out of the picker until an admin found
it. The cost tier is copied from GPT-6.1 Sol's row, the catalog model at the
same $2/$10 per M, or "medium" (the seeded default) if that row is missing. It
sorts after every existing row, so no administrator's ordering changes.

No other row is touched: no server-wide setting, conversation, turn, channel pin
or price. It runs before the deploy rolls the pods, and the previous build lists
catalog rows by walking its own catalog, so a row for a key it does not know is
invisible to it.

Revision ID: a3f6c9e2d4b8
Revises: d8e3f0a5b2c7
Create Date: 2026-10-16 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f6c9e2d4b8"
down_revision: Union[str, None] = "d8e3f0a5b2c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEY = "gemini-4-argon"
_PRICE_PEER = "gpt-6-1-sol"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " SELECT :key, true,"
            " COALESCE((SELECT cost_tier FROM chat_catalog_models"
            " WHERE model_key = :peer), 'medium'),"
            " COALESCE((SELECT MAX(sort_order) FROM chat_catalog_models), -1) + 1"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_KEY, peer=_PRICE_PEER)
    )


def downgrade() -> None:
    # The previous build does not know the key, so it cannot serve a
    # conversation or pin left on it either way; those selections are kept
    # and read as unavailable, and work again if this revision is reapplied.
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_KEY)
    )
