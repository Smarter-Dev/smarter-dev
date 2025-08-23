"""add_missing_points_earned_column_to_challenge_submissions

Revision ID: 6d7284be46fc
Revises: 5221f410cacb
Create Date: 2025-08-14 20:20:07.941133

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6d7284be46fc'
down_revision = '5221f410cacb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add points_earned column to challenge_submissions if it doesn't exist
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


def downgrade() -> None:
    # Remove points_earned column if it exists
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_submissions' 
                AND column_name = 'points_earned'
                AND table_schema = 'public'
            ) THEN
                ALTER TABLE challenge_submissions DROP COLUMN points_earned;
            END IF;
        END $$;
    """)