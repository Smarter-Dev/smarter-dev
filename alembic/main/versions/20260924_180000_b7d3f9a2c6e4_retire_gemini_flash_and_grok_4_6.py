"""admit Grok 4.7 beside 4.6, and make Gemini 3.8 Flash the only Flash

On 2026-09-24 Grok 4.7 replaces 4.6 at 20% less on every rate ($1.60/$4.80 per
M through OpenRouter), and Gemini 3.5 Flash Lite, 3.6 Flash and 3.7 Flash all
retire onto 3.8 Flash, which leaves it the one Gemini Flash in the catalog.
3.8 Flash takes 3.7 Flash's slot — the same class at the same promotional rate
— so 3.7 Flash is its direct predecessor; 4.6 is 4.7's.

It follows e5a9c3f7d2b8, the GPT-6 contract step, only so the chain keeps a
single head; it does not depend on anything that revision does.

This revision only adds, exactly as c8e2f4a6b1d9 did for GPT-6. It runs before
the deploy rolls the pods, so for a few minutes pods on the previous build read
the same tables. The previous build knows gemini-3-8-flash but not grok-4-7;
either way everything it reads is left as it was but the successors' own
``chat_catalog_models`` rows. Each successor's row carries its direct
predecessor's ``enabled``, ``cost_tier`` and ``sort_order``, enabled too if any
model it replaces was or the server default names one, and nothing else
changes. gemini-3-8-flash already has a row (it has been in the catalog since
2026-09-03, disabled in production); the copy overwrites it on purpose, since
3.8 Flash now stands in 3.7 Flash's slot. The retired rows stay because the
previous build's ``ensure_settings`` would otherwise seed them straight back.

The new build reads a stored retired key as its successor
(``model_catalog.successor_key``) wherever it is a *selection*: the seven
server-wide model columns of ``chat_settings`` (in production the summarizer
and compaction fallbacks both name 3.5 Flash Lite), a web conversation's
``selected_model_key``, a queued turn and the temporary default override. The
stored keys are rewritten, and the retired rows deleted, by a later contract
revision, which must ship in a later deploy, after this one has fully rolled.

Channel pins are never read through the successor map and never rewritten,
matching c8e2f4a6b1d9 (Zech, 2026-09-24): a channel that advertises its model
stops with a notice naming the dead key until an admin repins via
``/chat-bot-settings``. That covers all three pinned columns of
``channel_model_overrides`` — ``model_key``, ``fallback_model_key`` and
``drafter_model``.

Historical rows — ``web_chat_turns.model_key``, ``web_chat_model_changes``,
``web_chat_compactions``, ``usage_cost_rows`` — keep the key they ran under.
The retired price patches stay in ``llm_pricing`` and ``usage_invoice`` keeps
explicit provider mappings for the retired wire ids.

Revision ID: b7d3f9a2c6e4
Revises: e5a9c3f7d2b8
Create Date: 2026-09-24 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d3f9a2c6e4"
down_revision: Union[str, None] = "e5a9c3f7d2b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Retired key -> successor key; must match ``model_catalog.RETIRED_SUCCESSORS``.
# Each successor's first entry is its direct predecessor: the model whose slot
# it takes, and whose catalog row it copies.
_SUCCESSORS: tuple[tuple[str, str], ...] = (
    ("gemini-3-7-flash", "gemini-3-8-flash"),
    ("gemini-3-6-flash", "gemini-3-8-flash"),
    ("gemini-3-5-flash-lite", "gemini-3-8-flash"),
    ("grok-4-6", "grok-4-7"),
)


def _predecessors() -> dict[str, str]:
    """Successor key -> its direct predecessor (its first ``_SUCCESSORS`` entry)."""
    first: dict[str, str] = {}
    for retired_key, successor_key in _SUCCESSORS:
        first.setdefault(successor_key, retired_key)
    return first

# Successor keys the previous build does not know. Only these are unwound on
# downgrade; gemini-3-8-flash predates this revision.
_NEW_KEYS: frozenset[str] = frozenset({"grok-4-7"})

# (table, column) pairs holding a live selection: server-wide defaults and
# per-conversation picks, none of which a channel advertises. Only the downgrade
# writes them here. ``channel_model_overrides`` is excluded on purpose.
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


def _copy_catalog_row(from_key: str, to_key: str) -> None:
    """Give ``to_key`` the ``from_key`` row's settings, creating it if needed.

    A no-op when ``from_key`` has no row; ``ensure_settings`` then seeds
    ``to_key`` on its next load.
    """
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " SELECT :to_key, enabled, cost_tier, sort_order"
            " FROM chat_catalog_models WHERE model_key = :from_key"
            " ON CONFLICT (model_key) DO UPDATE SET"
            " enabled = EXCLUDED.enabled,"
            " cost_tier = EXCLUDED.cost_tier,"
            " sort_order = EXCLUDED.sort_order"
        ).bindparams(from_key=from_key, to_key=to_key)
    )


def upgrade() -> None:
    for successor_key, retired_key in _predecessors().items():
        _copy_catalog_row(retired_key, successor_key)
    for retired_key, successor_key in _SUCCESSORS:
        # A successor serves every selection of every model it replaces, so it
        # is enabled if any of them was — else a conversation left on an
        # enabled 3.5 Flash Lite would read as a disabled 3.8 Flash. The same
        # goes for the server default.
        op.execute(
            sa.text(
                "UPDATE chat_catalog_models SET enabled = true"
                " WHERE model_key = :successor_key AND ("
                " EXISTS (SELECT 1 FROM chat_catalog_models"
                " WHERE model_key = :retired_key AND enabled)"
                " OR EXISTS (SELECT 1 FROM chat_settings"
                " WHERE default_model_key = :retired_key))"
            ).bindparams(retired_key=retired_key, successor_key=successor_key)
        )


def downgrade() -> None:
    # Run with the new image, before the previous one is redeployed. Whatever an
    # admin set on a successor's row carries back to its direct predecessor's
    # row; the other retired rows were never touched.
    #
    # The previous build knows no grok-4-7, so every grok-4-7 selection —
    # including ones picked after the upgrade — moves back onto 4.6 and the
    # row goes. gemini-3-8-flash is different: the previous build carries it
    # as a model in its own right, and a selection stored on it cannot be told
    # apart from one made before the upgrade, so its selections and its row
    # stay. Channel pins are left alone here too; a channel repinned to
    # grok-4-7 stops with a notice under the previous build until an admin
    # repins it.
    for successor_key, retired_key in _predecessors().items():
        _copy_catalog_row(successor_key, retired_key)
        if successor_key not in _NEW_KEYS:
            continue
        for table, column in _SELECTION_COLUMNS:
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = :retired_key"
                    f" WHERE {column} = :successor_key"
                ).bindparams(retired_key=retired_key, successor_key=successor_key)
            )
        op.execute(
            sa.text(
                "DELETE FROM chat_catalog_models WHERE model_key = :successor_key"
            ).bindparams(successor_key=successor_key)
        )
    op.execute(
        sa.text(
            "UPDATE chat_catalog_models SET enabled = true"
            " WHERE model_key IN (SELECT default_model_key FROM chat_settings)"
        )
    )
