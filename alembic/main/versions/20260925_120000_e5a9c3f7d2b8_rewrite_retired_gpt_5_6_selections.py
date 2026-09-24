"""rewrite stored retired GPT-5.x selections, drop their catalog rows

The second half of c8e2f4a6b1d9. That revision gave GPT-6 Luna and Sol catalog
rows carrying the retired rows' settings and left every stored selection alone,
because pods on the previous build — which know no GPT-6 key — were still
reading them; the new build reads a retired selection as its successor
(``model_catalog.successor_key``). Ship this only in a deploy after that one has
fully rolled: every pod that can read these tables must know both GPT-6 keys.

Server-wide chat settings and web conversations' picks now store the successor
key, and the retired ``chat_catalog_models`` rows go. The successor rows are not
touched: they have been the live ones since c8e2f4a6b1d9, so whatever an admin
set on them stands. Whatever the server default now names is enabled, as
``validate_settings_input`` requires.

Channel pins are still never rewritten (Zech, 2026-09-24); a channel pinned to a
retired key keeps stopping with a notice until an admin repins it.

Revision ID: e5a9c3f7d2b8
Revises: c8e2f4a6b1d9
Create Date: 2026-09-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5a9c3f7d2b8"
down_revision: Union[str, None] = "c8e2f4a6b1d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Retired key -> successor key; must match c8e2f4a6b1d9 and
# ``model_catalog.RETIRED_SUCCESSORS``.
_SUCCESSORS: tuple[tuple[str, str], ...] = (
    ("gpt-5-6-luna", "gpt-6-luna"),
    ("gpt-5-6-terra", "gpt-6-sol"),
    ("gpt-5-4-mini", "gpt-6-luna"),
    ("gpt-5-4", "gpt-6-sol"),
    ("gpt-5-5", "gpt-6-sol"),
    ("gpt-5-6-sol", "gpt-6-sol"),
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
    # Back to c8e2f4a6b1d9's state: the retired rows return with their
    # successors' settings, so that revision's own downgrade has rows to carry
    # back to. Selections stay on GPT-6 keys, which the build between the two
    # revisions knows.
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
