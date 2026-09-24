"""admit GPT-6 Luna and Sol beside the GPT-5.x rows they replace

On 2026-09-24 GPT-6 replaces the two GPT-5.6 models the chat still carried:
Luna ($0.10/$0.50 per M) takes 5.6 Luna's cheap, fast slot — the server default
— and Sol ($2/$10) takes Terra's flagship slot. Both are served by OpenAI
directly; 5.6 Luna went through OpenRouter, and 6 Luna costs no more direct than
that did, so the OpenRouter hop and its fee go with it. The same day GPT-5.4,
5.4 Mini, 5.5 and 5.6 Sol retire too: Mini onto 6 Luna, the three flagships
onto 6 Sol, which undercuts all of them, and 5.4 Nano onto 6 Luna, since the
production OpenAI key now admits only the two GPT-6 models.

This revision only adds. It runs before the deploy rolls the pods, so for a
few minutes pods on the previous build — which know neither GPT-6 key — read
the same tables. Everything they read is therefore left as it was: each
successor gets a ``chat_catalog_models`` row carrying its direct predecessor's
(5.6 Luna's, Terra's) ``enabled``, ``cost_tier`` and ``sort_order``, enabled
too if any model it replaces was, and nothing else changes. The
previous build lists catalog rows by walking its own catalog, so a row for a key
it does not know is invisible to it. The retired rows stay because the previous
build's ``ensure_settings`` would otherwise seed them straight back, disabled
or — for 5.6 Luna, its default — enabled.

The new build reads a stored retired key as its successor
(``model_catalog.successor_key``) wherever it is a *selection*: the seven
server-wide model columns of ``chat_settings``, a web conversation's
``selected_model_key``, a queued turn and the temporary default override. So
nothing needs rewriting for the new build to serve GPT-6. The stored keys are
rewritten, and the retired rows deleted, by the following revision, which must
ship in a later deploy, after this one has fully rolled.

Channel pins are never read through the successor map and never rewritten,
matching f6b9d3e0a5c2, a7c2e5f1b8d4 and f3b8d1c6a4e9 (Zech, 2026-09-24): a
channel that advertises its model stops with a notice naming the dead key until
an admin repins via ``/chat-bot-settings``
(``ChannelEngine._unavailable_model_key``) rather than being switched to a model
nobody chose for it. That covers all three pinned columns of
``channel_model_overrides`` — ``model_key``, ``fallback_model_key`` and
``drafter_model``.

If the server default is a retired key, its successor row is enabled:
``validate_settings_input`` refuses to save while the default model is
disabled, and the new build reads the default as the successor.

Historical rows — ``web_chat_turns.model_key``, ``web_chat_model_changes``,
``web_chat_compactions``, ``usage_cost_rows`` — keep the key they ran under.
The GPT-5.6 price patches stay in ``llm_pricing`` and ``usage_invoice`` keeps
explicit provider mappings for the retired wire ids.

Revision ID: c8e2f4a6b1d9
Revises: f3b8d1c6a4e9
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e2f4a6b1d9"
down_revision: Union[str, None] = "f3b8d1c6a4e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Retired key -> successor key; must match ``model_catalog.RETIRED_SUCCESSORS``.
# Each successor's first entry is its direct predecessor: the model whose slot
# it takes, and whose catalog row it copies.
_SUCCESSORS: tuple[tuple[str, str], ...] = (
    ("gpt-5-6-luna", "gpt-6-luna"),
    ("gpt-5-6-terra", "gpt-6-sol"),
    ("gpt-5-4-mini", "gpt-6-luna"),
    ("gpt-5-4", "gpt-6-sol"),
    ("gpt-5-5", "gpt-6-sol"),
    ("gpt-5-6-sol", "gpt-6-sol"),
    ("gpt-5-4-nano", "gpt-6-luna"),
)


def _predecessors() -> dict[str, str]:
    """Successor key -> its direct predecessor (its first ``_SUCCESSORS`` entry)."""
    first: dict[str, str] = {}
    for retired_key, successor_key in _SUCCESSORS:
        first.setdefault(successor_key, retired_key)
    return first

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
        # enabled GPT-5.5 would read as a disabled GPT-6 Sol. The same goes for
        # the server default.
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
    # Run with the new image, before the previous one is redeployed: the
    # previous build knows no GPT-6 key. Every GPT-6 selection — including ones
    # picked after the upgrade — moves back onto the successor's direct
    # predecessor, and whatever an admin set on the GPT-6 row carries back to
    # that predecessor's row. The other retired rows were never touched. Channel
    # pins are left alone here too; a channel repinned to GPT-6 stops with a
    # notice under the previous build until an admin repins it.
    for successor_key, retired_key in _predecessors().items():
        for table, column in _SELECTION_COLUMNS:
            op.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = :retired_key"
                    f" WHERE {column} = :successor_key"
                ).bindparams(retired_key=retired_key, successor_key=successor_key)
            )
        _copy_catalog_row(successor_key, retired_key)
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
