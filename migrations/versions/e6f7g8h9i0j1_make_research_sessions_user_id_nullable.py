"""Make research_sessions.user_id nullable

Revision ID: e6f7g8h9i0j1
Revises: d5e6f7g8h9i0
Create Date: 2026-03-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e6f7g8h9i0j1"
down_revision: str = "d5e6f7g8h9i0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "research_sessions",
        "user_id",
        existing_type=sa.String(100),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "research_sessions",
        "user_id",
        existing_type=sa.String(100),
        nullable=False,
    )
