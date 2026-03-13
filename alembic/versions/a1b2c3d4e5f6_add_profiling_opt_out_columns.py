"""Add profiling opt-out columns to scan_user_profiles

Revision ID: a1b2c3d4e5f6
Revises: 14fccf6b9b7b
Create Date: 2026-03-13 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '14fccf6b9b7b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('scan_user_profiles', sa.Column('opt_out_narrative', sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column('scan_user_profiles', sa.Column('opt_out_technologies', sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column('scan_user_profiles', 'opt_out_technologies')
    op.drop_column('scan_user_profiles', 'opt_out_narrative')
