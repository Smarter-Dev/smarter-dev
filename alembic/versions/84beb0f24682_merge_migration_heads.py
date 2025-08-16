"""Merge migration heads

Revision ID: 84beb0f24682
Revises: ace931bfb26e, fb6d4e56b448
Create Date: 2025-08-16 07:16:30.306081

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '84beb0f24682'
down_revision = ('ace931bfb26e', 'fb6d4e56b448')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass