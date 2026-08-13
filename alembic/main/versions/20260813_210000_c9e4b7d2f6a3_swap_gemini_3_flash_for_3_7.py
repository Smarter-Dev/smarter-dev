"""retire gemini-3-flash for gemini-3-7-flash

Gemini 3.7 Flash shipped on 2026-08-13. The catalog is hard-capped at 24 by
Discord's 25-option select, so the oldest Flash comes out to make room: Gemini 3
Flash, which was also the last entry still pointing at a *preview* wire id.

3.7 is not a price increase. It carries the same promotional $0.75/$3.75 per M
as 3.6 Flash through 2026-12-31, so a channel moved from 3 Flash to 3.7 costs
the same as one already on 3.6.

Live selections move; channel pins deliberately do not, matching f6b9d3e0a5c2
and a7c2e5f1b8d4 — a channel that advertises its model stops with a notice
rather than being switched under its users.

``gemini-3-flash-preview`` stays in service outside the catalog (the resources
agent's reframer/gap-filler/author, and the blogging scout and research agents
all pin it directly), so its pricing patch and provider mapping remain live.

Revision ID: c9e4b7d2f6a3
Revises: a7c2e5f1b8d4
Create Date: 2026-08-13 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e4b7d2f6a3"
down_revision: Union[str, None] = "a7c2e5f1b8d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = "gemini-3-flash"
_NEW_KEY = "gemini-3-7-flash"

# Server-wide and per-conversation selections only. See the module docstring for
# why channel_model_overrides is excluded.
_SELECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("chat_settings", "default_model_key"),
    ("chat_settings", "summarizer_model_key"),
    ("chat_settings", "summarizer_fallback_model_key"),
    ("chat_settings", "compaction_model_key"),
    ("chat_settings", "compaction_fallback_model_key"),
    ("web_chat_conversations", "selected_model_key"),
)


def upgrade() -> None:
    for table, column in _SELECTION_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :to_key WHERE {column} = :from_key"
            ).bindparams(from_key=_OLD_KEY, to_key=_NEW_KEY)
        )
    # Seeded here rather than left to ensure_settings so it lands at "low" — it
    # is the same promotional rate as 3.6 Flash, which the outgoing 3 Flash also
    # shared, and the tier is what the Chat UI shows before a user picks.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, false, 'low', 7)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_NEW_KEY)
    )
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_OLD_KEY)
    )


def downgrade() -> None:
    # Restores the retired row without reversing the remap, which would drag
    # selections that always ran on 3.7 back onto the older model.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, false, 'low', 7)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_OLD_KEY)
    )
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_NEW_KEY)
    )
