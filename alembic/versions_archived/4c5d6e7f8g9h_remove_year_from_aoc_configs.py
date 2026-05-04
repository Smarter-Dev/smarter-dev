"""remove_year_from_aoc_configs

Revision ID: 4c5d6e7f8g9h
Revises: 79d84d4b60db
Create Date: 2025-11-30 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4c5d6e7f8g9h'
down_revision = '79d84d4b60db'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove year column from advent_of_code_configs
    # Year is now always derived from the current date at runtime
    op.drop_column('advent_of_code_configs', 'year')


def downgrade() -> None:
    # Re-add year column with default value
    op.add_column('advent_of_code_configs', sa.Column(
        'year',
        sa.Integer(),
        nullable=False,
        server_default='2025'
    ))
