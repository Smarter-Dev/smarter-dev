"""Make generation_script nullable

Revision ID: 0172624cf4b4
Revises: fe21ebb8050c
Create Date: 2025-08-16 07:21:26.290632

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0172624cf4b4'
down_revision = 'fe21ebb8050c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make generation_script nullable
    op.execute("""
        ALTER TABLE challenges 
        ALTER COLUMN generation_script DROP NOT NULL
    """)


def downgrade() -> None:
    # Make generation_script not nullable again
    op.execute("""
        ALTER TABLE challenges 
        ALTER COLUMN generation_script SET NOT NULL
    """)