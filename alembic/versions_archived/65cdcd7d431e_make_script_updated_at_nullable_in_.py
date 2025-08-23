"""Make script_updated_at nullable in challenges

Revision ID: 65cdcd7d431e
Revises: 0172624cf4b4
Create Date: 2025-08-16 07:22:36.031585

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '65cdcd7d431e'
down_revision = '0172624cf4b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if script_updated_at column exists and make it nullable
    op.execute("""
        DO $$ 
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenges' 
                AND column_name = 'script_updated_at'
            ) THEN
                ALTER TABLE challenges 
                ALTER COLUMN script_updated_at DROP NOT NULL;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Make script_updated_at not nullable again if it exists
    op.execute("""
        DO $$ 
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenges' 
                AND column_name = 'script_updated_at'
            ) THEN
                ALTER TABLE challenges 
                ALTER COLUMN script_updated_at SET NOT NULL;
            END IF;
        END $$;
    """)