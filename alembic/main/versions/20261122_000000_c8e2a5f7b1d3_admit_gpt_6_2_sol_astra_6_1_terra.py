"""admit GPT-6.2 Sol, GPT-6.2 Astra and GPT-6.1 Terra

#494 (2026-11-22). The catalog gains ``gpt-6-2-sol``, ``gpt-6-2-astra`` and
``gpt-6-1-terra``. Nothing is retired or moved. Each key gets a
``chat_catalog_models`` row inserted ``enabled = true``. The production OpenAI
key was granted access after the first draft, and every reasoning value and
image input was probed on it (6.2 Sol and Astra reject "none" and "minimal";
6.1 Terra rejects only "minimal").

Cost tiers follow price: Sol and Terra sit beside 6.1 Sol ("medium"), Astra at
four times Sol's rate is "high". Rows sort after every existing row. A row
that already exists is left alone, so an administrator's settings survive a
rerun.

Revision ID: c8e2a5f7b1d3
Revises: b4e7a2c9d1f6
Create Date: 2026-11-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e2a5f7b1d3"
down_revision: Union[str, None] = "b4e7a2c9d1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# key -> cost tier
_ADDED = (
    ("gpt-6-2-sol", "medium"),
    ("gpt-6-2-astra", "high"),
    ("gpt-6-1-terra", "medium"),
)


def upgrade() -> None:
    for key, tier in _ADDED:
        op.execute(
            sa.text(
                "INSERT INTO chat_catalog_models"
                " (model_key, enabled, cost_tier, sort_order)"
                " SELECT :key, true, :tier,"
                " COALESCE((SELECT MAX(sort_order) FROM chat_catalog_models), -1) + 1"
                " ON CONFLICT (model_key) DO NOTHING"
            ).bindparams(key=key, tier=tier)
        )


def downgrade() -> None:
    # The previous build does not know these keys, so a selection left on one
    # reads as unavailable either way; it works again if this revision is
    # reapplied.
    for key, _ in _ADDED:
        op.execute(
            sa.text(
                "DELETE FROM chat_catalog_models WHERE model_key = :key"
            ).bindparams(key=key)
        )
