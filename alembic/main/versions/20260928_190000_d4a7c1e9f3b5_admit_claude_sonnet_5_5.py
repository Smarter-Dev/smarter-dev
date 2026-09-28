"""admit Claude Sonnet 5.5 (via OpenRouter) to the chat catalog

Sonnet 5.5 joins the catalog on 2026-09-28 as a new key, ``claude-sonnet-5-5``,
served through OpenRouter. It replaces nothing: Sonnet 5 stays retired (it left
with the rest of the Claude family in f3b8d1c6a4e9), no stored selection moves
onto 5.5, and people pick it themselves (Zech, 2026-09-28).

This revision only adds its ``chat_catalog_models`` row, enabled so it is
selectable as soon as the new build serves it. ``ensure_settings`` would
otherwise seed it disabled, leaving it out of the picker until an admin found
it. The cost tier is copied from GPT-6 Sol's row, the model at the same
$2/$10 per M, or "medium" (the seeded default) if that row is missing. It sorts
after every existing row, so no administrator's ordering changes.

No other row is touched: no server-wide setting, conversation, turn, channel pin
or price. It runs before the deploy rolls the pods, and the previous build lists
catalog rows by walking its own catalog, so a row for a key it does not know is
invisible to it.

Revision ID: d4a7c1e9f3b5
Revises: c3e8a1d5f7b2
Create Date: 2026-09-28 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a7c1e9f3b5"
down_revision: Union[str, None] = "c3e8a1d5f7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEY = "claude-sonnet-5-5"
_PRICE_PEER = "gpt-6-sol"


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
