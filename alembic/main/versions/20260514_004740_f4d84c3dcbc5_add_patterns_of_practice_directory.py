"""add patterns-of-practice directory

Seeds the sixth /resources directory ("Patterns of Practice") into the existing
``resource_*`` tables. The schema doesn't change; this migration just re-runs
the idempotent ``seed_all`` so the new ``patterns-of-practice`` rows land.

Revision ID: f4d84c3dcbc5
Revises: 3265beda3e80
Create Date: 2026-05-14 00:47:40.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4d84c3dcbc5'
down_revision: Union[str, None] = '3265beda3e80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: every UPSERT keys on a stable unique constraint, so existing
    # rows for the other five directories are left alone while the new
    # patterns-of-practice rows are inserted.
    from smarter_dev.web._resources_seed import seed_all
    seed_all(op.get_bind())


def downgrade() -> None:
    # Track-key prefix `patterns:` is the stable marker for every row this
    # migration introduced. Delete in dependency order:
    # 1. faqs (references sources by string, not FK)
    # 2. sources (cascades into tool_sources / spine via FK)
    # 3. the directory itself (cascades into categories / tools / creators)
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM resource_faqs WHERE source_track_key LIKE 'patterns:%'"
    ))
    bind.execute(sa.text(
        "DELETE FROM resource_sources WHERE track_key LIKE 'patterns:%'"
    ))
    bind.execute(sa.text(
        "DELETE FROM resource_directories WHERE slug = 'patterns-of-practice'"
    ))
