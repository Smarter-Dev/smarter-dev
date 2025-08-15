"""ensure_challenge_submissions_has_standard_columns

Revision ID: ace931bfb26e
Revises: 6d7284be46fc
Create Date: 2025-08-14 20:34:26.462676

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ace931bfb26e'
down_revision = '6d7284be46fc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure all required columns exist in challenge_submissions table
    # This migration is idempotent and safe to run multiple times
    
    # Add points_earned column if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_submissions' 
                AND column_name = 'points_earned'
                AND table_schema = 'public'
            ) THEN
                ALTER TABLE challenge_submissions ADD COLUMN points_earned INTEGER;
            END IF;
        END $$;
    """)
    
    # Add created_at column if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_submissions' 
                AND column_name = 'created_at'
                AND table_schema = 'public'
            ) THEN
                ALTER TABLE challenge_submissions ADD COLUMN created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW();
            END IF;
        END $$;
    """)
    
    # Add updated_at column if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_submissions' 
                AND column_name = 'updated_at'
                AND table_schema = 'public'
            ) THEN
                ALTER TABLE challenge_submissions ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW();
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # This migration only adds missing columns, so downgrade is a no-op
    # The original table creation migrations handle the proper downgrade
    pass