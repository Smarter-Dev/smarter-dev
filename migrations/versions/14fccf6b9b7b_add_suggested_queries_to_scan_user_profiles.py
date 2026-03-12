"""Add suggested_queries to scan_user_profiles

Revision ID: 14fccf6b9b7b
Revises: ee0ad190fc72
Create Date: 2026-03-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON


# revision identifiers, used by Alembic.
revision = '14fccf6b9b7b'
down_revision = 'ee0ad190fc72'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('scan_user_profiles', sa.Column('suggested_queries', JSON, nullable=True))


def downgrade() -> None:
    op.drop_column('scan_user_profiles', 'suggested_queries')
