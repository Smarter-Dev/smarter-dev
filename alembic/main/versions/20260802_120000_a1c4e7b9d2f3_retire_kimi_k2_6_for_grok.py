"""remap kimi-k2-6 selections to kimi-k3

Kimi K2.6 (Digital Ocean) left the model catalog on 2026-08-02 to free the last
slot in Discord's 25-option model select for Grok 4.5. Kimi K3 on OpenCode Zen
is the same family's successor, so anything still *pointing* at the retired key
is remapped to it rather than silently falling back to the server default.

Only live selections move. Historical rows — ``web_chat_turns.model_key``,
``web_chat_model_changes``, ``web_chat_compactions``, ``usage_cost_rows`` — keep
the key they ran under; they record what actually happened and are priced
against the retired wire id, which llm_pricing still carries.

``chat_catalog_models`` is keyed by ``model_key``, so its retired row is deleted
rather than renamed — ``ensure_settings`` seeds a row for every catalog model it
is missing on the next load, ``kimi-k3`` included. Grok's row is seeded here
instead so it lands at the "high" cost tier rather than the generic default.

Revision ID: a1c4e7b9d2f3
Revises: f9b2d3e4a5c6
Create Date: 2026-08-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c4e7b9d2f3"
down_revision: Union[str, None] = "f9b2d3e4a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = "kimi-k2-6"
_NEW_KEY = "kimi-k3"

# (table, column) pairs holding a live model selection.
_SELECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("channel_model_overrides", "model_key"),
    ("channel_model_overrides", "fallback_model_key"),
    ("channel_model_overrides", "drafter_model"),
    ("chat_settings", "default_model_key"),
    ("chat_settings", "summarizer_model_key"),
    ("chat_settings", "summarizer_fallback_model_key"),
    ("chat_settings", "compaction_model_key"),
    ("chat_settings", "compaction_fallback_model_key"),
    ("web_chat_conversations", "selected_model_key"),
)


def _remap(from_key: str, to_key: str) -> None:
    for table, column in _SELECTION_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :to_key WHERE {column} = :from_key"
            ).bindparams(from_key=from_key, to_key=to_key)
        )
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :from_key"
        ).bindparams(from_key=from_key)
    )


def upgrade() -> None:
    _remap(_OLD_KEY, _NEW_KEY)
    # Seed Grok's catalog row explicitly. ``ensure_settings`` would create it on
    # the next admin load, but only at the generic "medium" tier — at $2/$6 per
    # M tokens Grok is the priciest non-flagship model we carry, and the tier is
    # what the Chat UI shows users before they pick.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES ('grok-4-5', false, 'high', 23)"
            " ON CONFLICT (model_key) DO NOTHING"
        )
    )


def downgrade() -> None:
    # Reversing the remap would also drag conversations that always ran on
    # Kimi K3 back onto the retired key, so the downgrade only restores the
    # catalog row; ensure_settings seeds it disabled at the medium cost tier.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, false, 'medium', 0)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_OLD_KEY)
    )
    op.execute(sa.text("DELETE FROM chat_catalog_models WHERE model_key = 'grok-4-5'"))
