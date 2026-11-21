"""admit Gemini 4 Hydrogen, Neon and Carbon; retire 3.8 Flash, 3.1 Pro, 4 Argon

#489 (2026-11-21). The catalog gains ``gemini-4-hydrogen``, ``gemini-4-neon`` and
``gemini-4-carbon`` and loses ``gemini-3-8-flash``, ``gemini-3-1-pro`` and
``gemini-4-argon``. Only ``chat_catalog_models`` rows change: the new keys are
inserted with the retired peer's enabled flag (default enabled) and cost
tier (else "medium"; they sort after every existing row), and the retired keys' rows
are deleted, as the retirement rule in ``model_catalog`` says. Also (scope change on #489: internal work runs on Claude credits) the
administrator settings that held the old defaults move: the summarizer and thread
evaluator from ``deepseek-v4`` to ``claude-haiku-5-5``, and any fallback slot
holding ``gemini-3-8-flash`` to ``claude-sonnet-5-5``. No conversation,
turn, channel pin, setting or price is touched; server-wide settings that still
hold a retired key read through ``RETIRED_SUCCESSORS``.

Revision ID: b4e7a2c9d1f6
Revises: f1a9c3d7b5e2
Create Date: 2026-11-21 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4e7a2c9d1f6"
down_revision: Union[str, None] = "f1a9c3d7b5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# new key -> retired key whose cost tier it inherits
_ADDED = (
    ("gemini-4-hydrogen", "gemini-3-8-flash"),
    ("gemini-4-neon", "gemini-3-1-pro"),
    ("gemini-4-carbon", "gemini-4-argon"),
)
_HAIKU = "claude-haiku-5-5"
_SONNET = "claude-sonnet-5-5"
_RETIRED = ("gemini-3-8-flash", "gemini-3-1-pro", "gemini-4-argon")


def upgrade() -> None:
    for key, peer in _ADDED:
        op.execute(
            sa.text(
                "INSERT INTO chat_catalog_models"
                " (model_key, enabled, cost_tier, sort_order)"
                " SELECT :key,"
                " COALESCE((SELECT enabled FROM chat_catalog_models"
                " WHERE model_key = :peer), true),"
                " COALESCE((SELECT cost_tier FROM chat_catalog_models"
                " WHERE model_key = :peer), 'medium'),"
                " COALESCE((SELECT MAX(sort_order) FROM chat_catalog_models), -1) + 1"
                " ON CONFLICT (model_key) DO NOTHING"
            ).bindparams(key=key, peer=peer)
        )
    for key in _RETIRED:
        op.execute(
            sa.text(
                "DELETE FROM chat_catalog_models WHERE model_key = :key"
            ).bindparams(key=key)
        )


    # The ORM default moved with the settings above; keep the column's own
    # default in step so a raw insert gets the same value.
    op.alter_column(
        "chat_settings",
        "thread_evaluator_model_key",
        server_default=sa.text(f"'{_HAIKU}'"),
    )
    for column in ("summarizer_model_key", "thread_evaluator_model_key"):
        op.execute(
            sa.text(
                f"UPDATE chat_settings SET {column} = :new WHERE {column} = :old"
            ).bindparams(new=_HAIKU, old="deepseek-v4")
        )
    for column in (
        "summarizer_fallback_model_key",
        "compaction_fallback_model_key",
        "thread_evaluator_fallback_model_key",
    ):
        op.execute(
            sa.text(
                f"UPDATE chat_settings SET {column} = :new WHERE {column} = :old"
            ).bindparams(new=_SONNET, old="gemini-3-8-flash")
        )


def downgrade() -> None:
    # Retired rows are not restored: ensure_settings re-seeds a row for any
    # key the restored build's catalog carries. Only the added rows go; the settings moves are not undone
    # (both models stay in the catalog). The server default does go back.
    op.alter_column(
        "chat_settings",
        "thread_evaluator_model_key",
        server_default=sa.text("'deepseek-v4'"),
    )
    for key, _ in _ADDED:
        op.execute(
            sa.text(
                "DELETE FROM chat_catalog_models WHERE model_key = :key"
            ).bindparams(key=key)
        )
