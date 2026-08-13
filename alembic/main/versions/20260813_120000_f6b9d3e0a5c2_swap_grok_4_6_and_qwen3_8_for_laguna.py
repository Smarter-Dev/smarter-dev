"""remap laguna-s and grok-4.5 selections onto their replacements

Three catalog changes land together on 2026-08-13: Grok 4.6 replaces Grok 4.5,
Qwen3.8 2.4T A95B joins on OpenRouter, and Laguna S 2.1 leaves — taking the
whole Poolside family with it.

Server-wide and per-conversation selections are remapped rather than left
dangling. Grok goes to its own successor. Laguna has no same-vendor successor,
so it goes to DeepSeek V4 Flash — the cheapest tool-capable model still in the
catalog ($0.14/$0.28 per M against Laguna's $0.10/$0.20), which is also the new
``DEFAULT_SUMMARIZER`` and therefore where Laguna's most common use lands
anyway.

``channel_model_overrides`` is deliberately NOT remapped. A channel is pinned to
a model on purpose and usually advertises which one it runs, so rewriting its
pin to a successor would make it answer as a model nobody chose and nobody was
told about. Those channels keep their dead key and the chat engine stops them
with a notice naming it, until an admin repins via ``/chat-bot-settings`` — see
``ChannelEngine._unavailable_model_key``. Expect affected channels to go quiet
on deploy; that is the intended outcome, not a regression.

Historical rows — ``web_chat_turns.model_key``, ``web_chat_model_changes``,
``web_chat_compactions``, ``usage_cost_rows`` — keep the key they ran under;
they record what actually happened and price against the retired wire ids,
which llm_pricing still carries.

``chat_catalog_models`` is keyed by ``model_key``, so retired rows are deleted
rather than renamed — ``ensure_settings`` seeds a row for every catalog model it
is missing on the next load. Both new models are seeded here instead so they
land at the "high" cost tier rather than the generic default: at $2/$6 per M
they are the priciest non-flagship models we carry, and the tier is what the
Chat UI shows before a user picks.

Revision ID: f6b9d3e0a5c2
Revises: e5a8c2d9f4b1
Create Date: 2026-08-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6b9d3e0a5c2"
down_revision: Union[str, None] = "e5a8c2d9f4b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GROK_OLD_KEY = "grok-4-5"
_GROK_NEW_KEY = "grok-4-6"
_LAGUNA_KEY = "poolside-laguna-s-2-1"
_LAGUNA_SUCCESSOR_KEY = "deepseek-v4"
_QWEN_KEY = "qwen3-8-2-4t"

# (table, column) pairs holding a live model selection that is safe to rewrite:
# server-wide defaults and per-conversation picks, none of which a channel
# advertises. ``channel_model_overrides`` is excluded on purpose — see above.
_SELECTION_COLUMNS: tuple[tuple[str, str], ...] = (
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


def _seed_catalog_row(key: str, cost_tier: str, sort_order: int) -> None:
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, false, :cost_tier, :sort_order)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=key, cost_tier=cost_tier, sort_order=sort_order)
    )


def upgrade() -> None:
    _remap(_GROK_OLD_KEY, _GROK_NEW_KEY)
    _remap(_LAGUNA_KEY, _LAGUNA_SUCCESSOR_KEY)
    _seed_catalog_row(_QWEN_KEY, "high", 22)
    _seed_catalog_row(_GROK_NEW_KEY, "high", 23)


def downgrade() -> None:
    # Reversing the remaps would also drag selections that always ran on the
    # successors back onto the retired keys, so the downgrade only restores the
    # catalog rows; ensure_settings seeds them disabled at the medium tier.
    _seed_catalog_row(_GROK_OLD_KEY, "high", 23)
    _seed_catalog_row(_LAGUNA_KEY, "low", 1)
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key IN (:qwen, :grok)"
        ).bindparams(qwen=_QWEN_KEY, grok=_GROK_NEW_KEY)
    )
