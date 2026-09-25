"""restore Gemini 3.5 Flash Lite to the chat catalog

b7d3f9a2c6e4 retired 3.5 Flash Lite onto 3.8 Flash with 3.6 and 3.7 Flash, and
7a18ff6495e1 then rewrote its stored selections and deleted its
``chat_catalog_models`` row. Only 3.5 Flash was meant to go (Zech,
2026-09-25), and that key, ``gemini-3-5-flash``, had left on 2026-07-21. The
new build lists 3.5 Flash Lite again and no longer reads it as 3.8 Flash.

This revision only puts its row back as it stood before the retirement:
enabled, "low", sort order 8 (a7c2e5f1b8d4 set those, and it was enabled in
production when it was retired). ``ensure_settings`` would otherwise seed it
disabled at "medium". It runs before the deploy rolls the pods; the previous
build reads the key as 3.8 Flash wherever it is a selection and carried the
same retired row between b7d3f9a2c6e4 and 7a18ff6495e1, so the extra row is
one it already tolerates.

No stored selection is rewritten. Channel pins on the key were never rewritten
and work again as soon as the new build serves it. The server-wide fallbacks and
web conversations that 7a18ff6495e1 moved to 3.8 Flash stay there: they cannot
be told apart from a later choice of 3.8 Flash here, so restoring any of them
is a separate decision. 3.6 and 3.7 Flash stay retired.

Revision ID: 479f5fca562d
Revises: 7a18ff6495e1
Create Date: 2026-09-25 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "479f5fca562d"
down_revision: Union[str, None] = "7a18ff6495e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEY = "gemini-3-5-flash-lite"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " VALUES (:key, true, 'low', 8)"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_KEY)
    )


def downgrade() -> None:
    # The previous build reads the key as 3.8 Flash; selections made on it
    # since keep working there, and 7a18ff6495e1 left no row for it.
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_KEY)
    )
