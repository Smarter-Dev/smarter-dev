"""Add slug to research_sessions and recent_queries to scan_user_profiles

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-03-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "j1k2l3m4n5o6"
down_revision: str = "i0j1k2l3m4n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "research_sessions",
        sa.Column("slug", sa.String(250), nullable=True, unique=True, index=True),
    )
    op.add_column(
        "scan_user_profiles",
        sa.Column("recent_queries", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scan_user_profiles", "recent_queries")
    op.drop_column("research_sessions", "slug")
