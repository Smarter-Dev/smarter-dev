"""move every GPT-6 Sol selection to GPT-6.1 Sol, drop 6 Sol's catalog row

The second half of 5cd5d3b96544. Retiring a model normally moves no selection
(``RETIRED_SUCCESSORS`` in ``model_catalog``); this one does, because the user
asked for everything on GPT-6 Sol to move to GPT-6.1 Sol (#37). It is an
exception for this key, not a pattern for the next retirement.

Ship this only in a deploy after 5cd5d3b96544's has fully rolled. It runs
before this deploy rolls its pods, and the pods it runs beside are on that
build, which knows both keys and so serves the moved selections at once.

Moved to ``gpt-6-1-sol`` wherever they name ``gpt-6-sol``:

- the seven server-wide model columns of ``chat_settings``;
- each web conversation's ``selected_model_key``;
- web chat turns that have not started (``submitted`` or ``queued``). A turn
  that is running, stopping or finished keeps the key it ran on;
- channel pins: ``model_key``, ``fallback_model_key`` and ``drafter_model``.

Reasoning levels are left as stored. 6.1 Sol has no ``none`` or ``minimal``;
``resolve_reasoning_level`` runs either as ``low``, its lowest level.

6 Sol's ``chat_catalog_models`` row goes. 6.1 Sol's row is not touched: it
has been live since 5cd5d3b96544, so whatever an administrator set on it
stands. Whatever the server default now names is enabled, as
``validate_settings_input`` requires. Until the rollout finishes, a pod on the
previous build may seed 6 Sol's row back, disabled, so it offers 6 Sol to
nobody; the new build ignores a row for a key it does not know.

History keeps the model it ran on: finished turns, ``web_chat_model_changes``,
``web_chat_compactions``, usage rows and every ``model_name`` log column.

Every statement matches only rows still naming ``gpt-6-sol``, so running it
again moves only what was picked since.

Revision ID: 7c2d9e4b1a60
Revises: 5cd5d3b96544
Create Date: 2026-09-29 22:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c2d9e4b1a60"
down_revision: str | None = "5cd5d3b96544"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETIRED = "gpt-6-sol"
_SUCCESSOR = "gpt-6-1-sol"

# (table, column) pairs holding a live selection.
_SELECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("chat_settings", "default_model_key"),
    ("chat_settings", "summarizer_model_key"),
    ("chat_settings", "summarizer_fallback_model_key"),
    ("chat_settings", "compaction_model_key"),
    ("chat_settings", "compaction_fallback_model_key"),
    ("chat_settings", "thread_evaluator_model_key"),
    ("chat_settings", "thread_evaluator_fallback_model_key"),
    ("web_chat_conversations", "selected_model_key"),
    ("channel_model_overrides", "model_key"),
    ("channel_model_overrides", "fallback_model_key"),
    ("channel_model_overrides", "drafter_model"),
)

# Turns that have not reached a model yet.
_UNSTARTED_TURN_STATUSES: tuple[str, ...] = ("submitted", "queued")


def upgrade() -> None:
    for table, column in _SELECTION_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :successor"
                f" WHERE {column} = :retired"
            ).bindparams(retired=_RETIRED, successor=_SUCCESSOR)
        )
    op.execute(
        sa.text(
            "UPDATE web_chat_turns SET model_key = :successor"
            " WHERE model_key = :retired AND status IN :statuses"
        ).bindparams(
            sa.bindparam("statuses", expanding=True),
            retired=_RETIRED,
            successor=_SUCCESSOR,
            statuses=list(_UNSTARTED_TURN_STATUSES),
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :retired"
        ).bindparams(retired=_RETIRED)
    )
    op.execute(
        sa.text(
            "UPDATE chat_catalog_models SET enabled = true"
            " WHERE model_key IN (SELECT default_model_key FROM chat_settings)"
        )
    )


def downgrade() -> None:
    # Back to 5cd5d3b96544's state: 6 Sol's row returns with 6.1 Sol's
    # settings. Selections stay on 6.1 Sol, which that build serves, so no
    # choice made since the upgrade is undone.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " SELECT :retired, enabled, cost_tier, sort_order"
            " FROM chat_catalog_models WHERE model_key = :successor"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(retired=_RETIRED, successor=_SUCCESSOR)
    )
