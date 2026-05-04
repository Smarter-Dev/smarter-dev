"""Add email confirmation to campaign_signups

Revision ID: a1b2c3d4e5f6
Revises: 8c3f1d255ee2
Create Date: 2026-02-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '8c3f1d255ee2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('campaign_signups', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('email_confirmed', sa.Boolean(), nullable=False, server_default='false')
        )
        batch_op.add_column(
            sa.Column('confirmation_token', sa.String(length=36), nullable=True)
        )
        batch_op.create_index(
            batch_op.f('ix_campaign_signups_confirmation_token'),
            ['confirmation_token'],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('campaign_signups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_campaign_signups_confirmation_token'))
        batch_op.drop_column('confirmation_token')
        batch_op.drop_column('email_confirmed')
