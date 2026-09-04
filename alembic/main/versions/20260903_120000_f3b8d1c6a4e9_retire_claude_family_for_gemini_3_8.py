"""retire the Claude family and admit gemini-3-8-flash

Two catalog changes land together on 2026-09-03. Gemini 3.8 Flash becomes the
proactive agent's model and joins the catalog; the whole Claude family leaves,
because nobody on the server talks to it and it was holding three of the 24
Discord select slots.

Admitting 3.8 Flash is not only about the picker. ``_normalized_model_identity``
resolves a wire id to a provider *through the catalog*, so an uncatalogued
proactive model files the agent's entire spend under provider "unknown" in the
invoice breakdown. Its cost was already correct either way — llm_pricing carries
the rate — but the attribution was not.

Live selections move; channel pins deliberately do not, matching f6b9d3e0a5c2,
a7c2e5f1b8d4 and c9e4b7d2f6a3 — a channel that advertises its model stops with a
notice naming the dead key until an admin repins via ``/chat-bot-settings``
(``ChannelEngine._unavailable_model_key``), rather than being switched to a
model nobody chose and nobody was told about.

Claude has no same-vendor successor, so the remap goes by class and by rate:
Opus 5 and Sonnet 5 both land on GPT-5.6 Terra, the only flagship the catalog
still carries, whose $2/$12 per M is the nearest rate to Sonnet's $2/$10. Haiku
4.5 lands on GPT-5.6 Luna, the cheap fast tier and the server default, which is
where Haiku's use would go anyway.

If the server-wide default itself was a Claude key, its successor row is also
enabled: ``validate_settings_input`` refuses to save while the default model is
disabled, and a silently unsaveable admin form is a worse outcome than an
extra enabled model.

Historical rows — ``web_chat_turns.model_key``, ``web_chat_model_changes``,
``web_chat_compactions``, ``usage_cost_rows`` — keep the key they ran under.
The Claude price patches stay in ``llm_pricing`` so those rows keep pricing, and
``usage_invoice`` gains explicit provider mappings for the three retired wire
ids so their spend stays under "Anthropic" instead of falling to "unknown".

``chat_catalog_models`` is keyed by ``model_key``, so retired rows are deleted
rather than renamed — ``ensure_settings`` seeds a row for every catalog model it
is missing on the next load. 3.8 Flash is seeded here instead so it lands at the
"low" cost tier rather than the generic default: it carries the same promotional
$0.75/$3.75 per M as 3.6 and 3.7 Flash, and the tier is what the Chat UI shows
before a user picks.

Revision ID: f3b8d1c6a4e9
Revises: e2a6b9c4d7f1
Create Date: 2026-09-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3b8d1c6a4e9"
down_revision: Union[str, None] = "e2a6b9c4d7f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEMINI_3_8_KEY = "gemini-3-8-flash"
_FLAGSHIP_SUCCESSOR_KEY = "gpt-5-6-terra"
_FAST_SUCCESSOR_KEY = "gpt-5-6-luna"

# Retired key -> where its live selections go. See the module docstring for why
# each successor was chosen.
_CLAUDE_SUCCESSORS: tuple[tuple[str, str], ...] = (
    ("claude-opus-5", _FLAGSHIP_SUCCESSOR_KEY),
    ("claude-sonnet-5", _FLAGSHIP_SUCCESSOR_KEY),
    ("claude-haiku-4-5", _FAST_SUCCESSOR_KEY),
)

# (table, column) pairs holding a live model selection that is safe to rewrite:
# server-wide defaults and per-conversation picks, none of which a channel
# advertises. ``channel_model_overrides`` is excluded on purpose — see above.
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
    _seed_catalog_row(_GEMINI_3_8_KEY, "low", 9)
    for retired_key, successor_key in _CLAUDE_SUCCESSORS:
        _remap(retired_key, successor_key)
    # ``validate_settings_input`` refuses to save while the default model is
    # disabled, so a server whose default was a Claude key would land on a
    # successor row that is merely present, and its admin form would be
    # unsaveable until someone guessed why. Enable whatever the default now
    # names. Idempotent, and a no-op on the far more likely case where the
    # default was never a Claude model.
    op.execute(
        sa.text(
            "UPDATE chat_catalog_models SET enabled = true"
            " WHERE model_key IN (SELECT default_model_key FROM chat_settings)"
        )
    )


def downgrade() -> None:
    # Reversing the remaps would also drag selections that always ran on the
    # successors back onto the retired keys, so the downgrade only restores the
    # catalog rows; ensure_settings re-seeds anything else it finds missing.
    _seed_catalog_row("claude-opus-5", "high", 19)
    _seed_catalog_row("claude-haiku-4-5", "medium", 20)
    _seed_catalog_row("claude-sonnet-5", "high", 21)
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_GEMINI_3_8_KEY)
    )
