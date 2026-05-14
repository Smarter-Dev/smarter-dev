"""add agent-engineering-patterns directory

Seeds the seventh /resources directory ("Patterns for the Age of Agents") into
the existing ``resource_*`` tables. No schema change; this migration just
re-runs the idempotent ``seed_all`` so the new ``agent-engineering-patterns``
rows land.

Revision ID: bef52194dfc9
Revises: f4d84c3dcbc5
Create Date: 2026-05-14 02:39:31.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bef52194dfc9'
down_revision: Union[str, None] = 'f4d84c3dcbc5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: every UPSERT keys on a stable unique constraint, so existing
    # rows for the other six directories are left alone while the new
    # agent-engineering-patterns rows are inserted. The same call also
    # re-applies the Patterns of Practice content changes (renamed categories,
    # Builder URL fix) since those are part of the same idempotent seed.
    from smarter_dev.web._resources_seed import seed_all
    bind = op.get_bind()
    seed_all(bind)

    # The seed is add-or-update only; it doesn't delete categories that were
    # removed from the data. The three "Age of Agents" sub-categories that
    # lived under Patterns of Practice now belong to the new directory, so
    # remove the old rows here. Child resource_tools / resource_tool_sources
    # cascade via FK.
    bind.execute(sa.text(
        """
        DELETE FROM resource_categories
        WHERE directory_id = (
            SELECT id FROM resource_directories WHERE slug = 'patterns-of-practice'
        )
        AND slug IN ('agents-spec-first', 'agents-verification', 'agents-human-loop')
        """
    ))


def downgrade() -> None:
    # Track-key prefix `agent-patterns:` is the stable marker for every row
    # this migration introduced. Delete in dependency order:
    # 1. faqs (references sources by string, not FK)
    # 2. sources (cascades into tool_sources / spine via FK)
    # 3. the directory itself (cascades into categories / tools / creators)
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM resource_faqs WHERE source_track_key LIKE 'agent-patterns:%'"
    ))
    bind.execute(sa.text(
        "DELETE FROM resource_sources WHERE track_key LIKE 'agent-patterns:%'"
    ))
    bind.execute(sa.text(
        "DELETE FROM resource_directories WHERE slug = 'agent-engineering-patterns'"
    ))
