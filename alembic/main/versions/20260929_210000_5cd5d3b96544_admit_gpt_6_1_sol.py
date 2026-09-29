"""admit GPT-6.1 Sol beside GPT-6 Sol, which it replaces

GPT-6.1 Sol joins the catalog on 2026-09-29 as ``gpt-6-1-sol`` (wire id
``gpt-6.1-sol``, served by OpenAI directly) and replaces GPT-6 Sol. Unlike an
ordinary retirement, which moves no selection (see ``RETIRED_SUCCESSORS`` in
``model_catalog``), the user asked for everything on 6 Sol to move to 6.1 Sol
(#37). That is an exception for this one key, not a change to the rule.

The move takes two deploys. This revision runs before the first one rolls its
pods, and only adds: 6.1 Sol gets a ``chat_catalog_models`` row carrying 6 Sol's
``enabled``, ``cost_tier`` and ``sort_order``, so it sits where 6 Sol does. No
stored selection changes, so pods on the previous build, which do not know the
new key, keep serving 6 Sol, and so does the new build, which knows both. The
following revision moves the selections and drops 6 Sol's row, in a later
deploy once every pod knows 6.1 Sol.

With no 6 Sol row the new row is enabled at the seeded "medium" cost tier,
after every existing row. A row that already exists is left alone, so an
administrator's settings survive a rerun.

Revision ID: 5cd5d3b96544
Revises: d4a7c1e9f3b5
Create Date: 2026-09-29 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5cd5d3b96544"
down_revision: Union[str, None] = "d4a7c1e9f3b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEY = "gpt-6-1-sol"
_PREDECESSOR = "gpt-6-sol"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO chat_catalog_models"
            " (model_key, enabled, cost_tier, sort_order)"
            " SELECT :key,"
            " COALESCE((SELECT enabled FROM chat_catalog_models"
            " WHERE model_key = :predecessor), true),"
            " COALESCE((SELECT cost_tier FROM chat_catalog_models"
            " WHERE model_key = :predecessor), 'medium'),"
            " COALESCE((SELECT sort_order FROM chat_catalog_models"
            " WHERE model_key = :predecessor),"
            " (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM chat_catalog_models))"
            " ON CONFLICT (model_key) DO NOTHING"
        ).bindparams(key=_KEY, predecessor=_PREDECESSOR)
    )


def downgrade() -> None:
    # The previous build does not know the key, so it cannot serve a
    # selection left on it either way; any picked since are kept and read as
    # unavailable, and work again if this revision is reapplied.
    op.execute(
        sa.text(
            "DELETE FROM chat_catalog_models WHERE model_key = :key"
        ).bindparams(key=_KEY)
    )
