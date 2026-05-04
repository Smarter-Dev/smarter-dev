"""Add cost tracking columns to research_sessions

Revision ID: f7g8h9i0j1k2
Revises: e6f7g8h9i0j1
Create Date: 2026-03-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f7g8h9i0j1k2"
down_revision: str = "e6f7g8h9i0j1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "research_sessions",
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_sessions",
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_sessions",
        sa.Column("model_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "research_sessions",
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_sessions", "cost_usd")
    op.drop_column("research_sessions", "model_name")
    op.drop_column("research_sessions", "cache_write_tokens")
    op.drop_column("research_sessions", "cache_read_tokens")
