"""add campaign_signups table

Revision ID: 8c3f1d255ee2
Revises: 0ee3e9c6d226
Create Date: 2026-02-25 20:16:33.691588

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '8c3f1d255ee2'
down_revision = '0ee3e9c6d226'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('campaign_signups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('campaign_slug', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('discord_id', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('email IS NOT NULL OR discord_id IS NOT NULL', name=op.f('ck_campaign_signups_at_least_one_contact')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_campaign_signups')),
    sa.UniqueConstraint('campaign_slug', 'discord_id', name='uq_campaign_signups_slug_discord_id'),
    sa.UniqueConstraint('campaign_slug', 'email', name='uq_campaign_signups_slug_email')
    )
    with op.batch_alter_table('campaign_signups', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_campaign_signups_campaign_slug'), ['campaign_slug'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('campaign_signups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_campaign_signups_campaign_slug'))

    op.drop_table('campaign_signups')
