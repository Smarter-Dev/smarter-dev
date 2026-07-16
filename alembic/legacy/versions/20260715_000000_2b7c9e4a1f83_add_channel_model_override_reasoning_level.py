"""add reasoning_level to channel_model_overrides

Revision ID: 2b7c9e4a1f83
Revises: 1540d3a18dc4
Create Date: 2026-07-15 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b7c9e4a1f83'
down_revision: str | None = '1540d3a18dc4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Admin-selected reasoning/thinking effort for the channel's model.
    # NULL means "use the model's default level".
    op.add_column(
        'channel_model_overrides',
        sa.Column('reasoning_level', sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('channel_model_overrides', 'reasoning_level')
