"""retire gemini-3-1-flash-lite, add deepseek-v4-pro

Gemini 3.1 Flash Lite leaves the catalog on 2026-08-13, superseded in its own
class by 3.5 Flash Lite (3.6 Flash is a different class, not a replacement).
That frees the last of the 24 slots Discord's model select allows, and DeepSeek
V4 Pro takes it.

The same migration moves the default model to GPT-5.6 Luna and drops Gemini to
the fallback role behind it. Luna is the cheaper of the two as well as the
better default — $0.10/$0.60 per M through OpenRouter against 3.5 Flash Lite's
$0.30/$2.50 — and a fallback on a different vendor is worth more than one
sharing an upstream with the model it backstops.

This retirement is unlike the others in this series: the key being retired is
the SERVER DEFAULT. ``chat_settings.default_model_key`` cannot be left dangling
the way a channel pin can — ``validate_settings_input`` rejects an unknown key
outright, and it also rejects a default that is not enabled in
``chat_catalog_models``. So the order below matters: the default moves to Luna
first, then the remaining 3.1 references follow their successor, and both the
new default and the new fallback are left enabled and selectable.

One visible consequence: prod previously had exactly one enabled model, and
comes out of this with two (Luna and 3.5 Flash Lite), so the Chat UI offers a
choice where it used to offer none.

Channel pins are still deliberately NOT remapped, matching f6b9d3e0a5c2: a
channel that advertises its model stops with a notice instead of being switched
under its users.

The wire id ``gemini-3.1-flash-lite`` stays in service — title generation, media
reading, image prompt review and the blogging agents pin it directly rather than
through the catalog — so its pricing patch and provider mapping remain live
rather than historical.

Revision ID: a7c2e5f1b8d4
Revises: f6b9d3e0a5c2
Create Date: 2026-08-13 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c2e5f1b8d4"
down_revision: Union[str, None] = "f6b9d3e0a5c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = "gemini-3-1-flash-lite"
_NEW_KEY = "gemini-3-5-flash-lite"
_PRO_KEY = "deepseek-v4-pro"
_DEFAULT_KEY = "gpt-5-6-luna"

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


def _remap(from_key: str, to_key: str) -> None:
    for table, column in _SELECTION_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :to_key WHERE {column} = :from_key"
            ).bindparams(from_key=from_key, to_key=to_key)
        )


def upgrade() -> None:
    # Luna becomes the model that answers by default, and Gemini drops to the
    # fallback role behind it. Guarded on the outgoing seeded value so an admin
    # who deliberately chose a different default keeps it — this migration is
    # correcting a seed, not overriding a decision. Runs BEFORE the remap below,
    # which would otherwise rewrite this same column to the 3.5 successor.
    op.execute(
        sa.text(
            "UPDATE chat_settings SET default_model_key = :luna"
            " WHERE default_model_key = :seeded"
        ).bindparams(luna=_DEFAULT_KEY, seeded=_OLD_KEY)
    )
    # The default has to be selectable or validate_settings_input rejects the
    # settings form outright.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, true, 'low', 16)"
            " ON CONFLICT (model_key)"
            " DO UPDATE SET enabled = true, cost_tier = 'low'"
        ).bindparams(key=_DEFAULT_KEY)
    )
    # Everything still pointing at 3.1 — summarizer/compaction fallbacks, live
    # conversations — moves to its in-class successor, which is what puts Gemini
    # in the fallback slots.
    _remap(_OLD_KEY, _NEW_KEY)
    # The incoming default has to be selectable, and inherits the outgoing
    # default's "low" tier — it is the same model class at a similar price, and
    # the tier is what the Chat UI shows before a user picks. ensure_settings
    # would seed this row only if missing, and only ever disabled at "medium".
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, true, 'low', 8)"
            " ON CONFLICT (model_key)"
            " DO UPDATE SET enabled = true, cost_tier = 'low'"
        ).bindparams(key=_NEW_KEY)
    )
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_OLD_KEY)
    )
    # Pro seeds disabled at "medium": at $0.4225/$0.845 measured it is far from
    # the priciest model we carry, but it is several times the Flash sibling
    # admins may assume they are picking.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, false, 'medium', 7)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_PRO_KEY)
    )


def downgrade() -> None:
    # Restores the retired row so the old default is selectable again, but does
    # not reverse the remap: that would drag conversations which always ran on
    # 3.5 Flash Lite back onto 3.1.
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, true, 'low', 0)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_OLD_KEY)
    )
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_PRO_KEY)
    )
