"""move web Chat's configured compaction model from GPT-6 Luna to Claude Haiku 5.5

Zech moved every non-chat use of GPT-6 Luna to Claude Haiku 5.5 on 2026-10-17
(#199), chat compaction included; Chat itself stays on Luna. Discord's
compactor is a code default (``chat_compaction.DEFAULT_COMPACT_MODEL``), but web
Chat's lives in ``chat_settings.compaction_model_key``, which the admin settings
page writes, so a new default in ``web/chat/settings.py`` reaches only a fresh
database. This moves the live row.

Only a row still naming ``gpt-6-luna`` moves: any other choice an administrator
made stands. The fallback (Gemini 3.8 Flash) is left alone, and so are the
modes that compact on the conversation's own model (``configured_compaction``
false). Haiku 5.5 needs no ``chat_catalog_models`` row to serve as an auxiliary
model; ``validate_settings_input`` asks only that it be priced.

Revision ID: d8a3f1c6e9b2
Revises: c5e1a8d3f7b2
Create Date: 2026-10-17 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8a3f1c6e9b2"
down_revision: str | None = "c5e1a8d3f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LUNA = "gpt-6-luna"
_HAIKU = "claude-haiku-5-5"


def _move(old: str, new: str) -> None:
    op.execute(
        sa.text(
            "UPDATE chat_settings SET compaction_model_key = :new"
            " WHERE compaction_model_key = :old"
        ).bindparams(old=old, new=new)
    )


def upgrade() -> None:
    _move(_LUNA, _HAIKU)


def downgrade() -> None:
    _move(_HAIKU, _LUNA)
