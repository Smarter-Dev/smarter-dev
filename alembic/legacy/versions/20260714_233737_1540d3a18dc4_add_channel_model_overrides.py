"""add channel_model_overrides

Revision ID: 1540d3a18dc4
Revises: 10eb241de73b
Create Date: 2026-07-14 23:37:37.395167

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1540d3a18dc4'
down_revision: str | None = '10eb241de73b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per-channel admin model override + token budgets (one row per channel).
    op.create_table('channel_model_overrides',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('guild_id', sa.String(length=20), nullable=False),
    sa.Column('channel_id', sa.String(length=20), nullable=False),
    sa.Column('model_key', sa.String(length=64), nullable=False),
    sa.Column('daily_token_budget', sa.Integer(), server_default='0', nullable=False),
    sa.Column('hourly_token_budget', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_channel_model_overrides')),
    sa.UniqueConstraint('channel_id', name='uq_channel_model_overrides_channel_id')
    )
    op.create_index('ix_channel_model_overrides_guild_id', 'channel_model_overrides', ['guild_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_channel_model_overrides_guild_id', table_name='channel_model_overrides')
    op.drop_table('channel_model_overrides')
