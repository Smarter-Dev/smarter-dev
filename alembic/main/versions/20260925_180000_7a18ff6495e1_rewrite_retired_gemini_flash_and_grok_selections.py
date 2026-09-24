"""rewrite stored retired Gemini Flash and Grok 4.6 selections, drop their rows

The second half of b7d3f9a2c6e4, as e5a9c3f7d2b8 is of c8e2f4a6b1d9. That
revision gave Grok 4.7 a catalog row and 3.8 Flash 3.7 Flash's slot and left
every stored selection alone, because pods on the previous build — which knows
no grok-4-7 — were still reading them; the new build reads a retired selection
as its successor (``model_catalog.successor_key``). Ship this only in a deploy
after that one has fully rolled: every pod that can read these tables must know
grok-4-7.

Server-wide chat settings and web conversations' picks now store the successor
key, and the retired ``chat_catalog_models`` rows go; ``ensure_settings`` seeds
from ``MODEL_CATALOG``, which no longer lists them, so they stay gone. The
successor rows are not touched: they have been the live ones since
b7d3f9a2c6e4, so whatever an admin set on them stands. Whatever the server
default now names is enabled, as ``validate_settings_input`` requires.

Queued turns and the temporary default override keep being read through the
successor map, as after e5a9c3f7d2b8; historical rows keep the key they ran
under.

Channel pins are still never rewritten (Zech, 2026-09-24); a channel pinned to a
retired key keeps stopping with a notice until an admin repins it.

Revision ID: 7a18ff6495e1
Revises: b7d3f9a2c6e4
Create Date: 2026-09-25 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a18ff6495e1"
down_revision: Union[str, None] = "b7d3f9a2c6e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Retired key -> successor key; must match b7d3f9a2c6e4 and
# ``model_catalog.RETIRED_SUCCESSORS``.
_SUCCESSORS: tuple[tuple[str, str], ...] = (
    ("gemini-3-7-flash", "gemini-3-8-flash"),
    ("gemini-3-6-flash", "gemini-3-8-flash"),
    ("gemini-3-5-flash-lite", "gemini-3-8-flash"),
    ("grok-4-6", "grok-4-7"),
)

# Live selections only. ``channel_model_overrides`` is excluded on purpose.
_SELECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("chat_settings", "default_model_key"),
    ("chat_settings", "summarizer_model_key"),
    ("chat_settings", "summarizer_fallback_model_key"),
    ("chat_settings", "compaction_model_key"),
    ("chat_settings", "compaction_fallback_model_key"),
    ("chat_settings", "thread_evaluator_model_key"),
    ("chat_settings", "thread_evaluator_fallback_model_key"),
    ("web_chat_conversations", "selected_model_key"),
)


def upgrade() -> None:
    for retired_key, successor_key in _SUCCESSORS:
        for table, column in _SELECTION_COLUMNS:
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = :successor_key"
                    f" WHERE {column} = :retired_key"
                ).bindparams(retired_key=retired_key, successor_key=successor_key)
            )
        op.execute(
            sa.text(
                "DELETE FROM chat_catalog_models WHERE model_key = :retired_key"
            ).bindparams(retired_key=retired_key)
        )
    op.execute(
        sa.text(
            "UPDATE chat_catalog_models SET enabled = true"
            " WHERE model_key IN (SELECT default_model_key FROM chat_settings)"
        )
    )


def downgrade() -> None:
    # Back to b7d3f9a2c6e4's state: the retired rows return with their
    # successors' settings, so that revision's own downgrade has rows to carry
    # back to. Their own pre-retirement settings are gone, so 3.5 Flash Lite and
    # 3.6 Flash come back as copies of 3.8 Flash, as e5a9c3f7d2b8's rows come
    # back as copies of GPT-6. Selections stay on the successor keys, which the
    # build between the two revisions knows.
    for retired_key, successor_key in _SUCCESSORS:
        op.execute(
            sa.text(
                "INSERT INTO chat_catalog_models"
                " (model_key, enabled, cost_tier, sort_order)"
                " SELECT :retired_key, enabled, cost_tier, sort_order"
                " FROM chat_catalog_models WHERE model_key = :successor_key"
                " ON CONFLICT (model_key) DO NOTHING"
            ).bindparams(retired_key=retired_key, successor_key=successor_key)
        )
