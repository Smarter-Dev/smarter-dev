"""add jina cache columns to resource_sources

Adds two columns to ``resource_sources`` so we can persist the Jina Reader
body once per curated URL and re-serve it from Postgres on subsequent agent
runs. Without this every ``read_source`` call burned a Jina request; with
it, a one-time precrawl warms the catalog and the agent's reads become DB
hits.

``jina_content`` holds the truncated body (up to ~10 k chars, matching the
runtime cap in ``smarter_dev/web/resources_agent.py``). ``jina_fetched_at``
drives a 30-day staleness check.

Revision ID: 3265beda3e80
Revises: 53c59fcec17b
Create Date: 2026-05-13 20:22:07.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3265beda3e80'
down_revision: Union[str, None] = '53c59fcec17b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'resource_sources',
        sa.Column('jina_content', sa.Text(), nullable=True),
    )
    op.add_column(
        'resource_sources',
        sa.Column('jina_fetched_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('resource_sources', 'jina_fetched_at')
    op.drop_column('resource_sources', 'jina_content')
