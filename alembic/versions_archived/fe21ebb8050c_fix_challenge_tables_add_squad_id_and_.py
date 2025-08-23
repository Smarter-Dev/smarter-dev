"""Fix challenge tables add squad_id and generation_script

Revision ID: fe21ebb8050c
Revises: 84beb0f24682
Create Date: 2025-08-16 07:16:35.409960

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fe21ebb8050c'
down_revision = '84beb0f24682'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add squad_id column to challenge_submissions if it doesn't exist
    op.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_submissions' 
                AND column_name = 'squad_id'
            ) THEN
                ALTER TABLE challenge_submissions 
                ADD COLUMN squad_id UUID;
                
                -- Add foreign key constraint
                ALTER TABLE challenge_submissions
                ADD CONSTRAINT fk_challenge_submissions_squad_id
                FOREIGN KEY (squad_id) REFERENCES squads(id) ON DELETE CASCADE;
                
                -- Create index
                CREATE INDEX ix_challenge_submissions_squad_id 
                ON challenge_submissions(squad_id);
            END IF;
        END $$;
    """)
    
    # Add generation_script column to challenges if it doesn't exist
    op.execute("""
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenges' 
                AND column_name = 'generation_script'
            ) THEN
                ALTER TABLE challenges 
                ADD COLUMN generation_script TEXT;
                
                -- Copy data from input_generator_script if it exists
                UPDATE challenges 
                SET generation_script = input_generator_script
                WHERE generation_script IS NULL 
                AND input_generator_script IS NOT NULL;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Remove generation_script column
    op.execute("""
        ALTER TABLE challenges 
        DROP COLUMN IF EXISTS generation_script;
    """)
    
    # Remove squad_id column and its constraints
    op.execute("""
        ALTER TABLE challenge_submissions 
        DROP CONSTRAINT IF EXISTS fk_challenge_submissions_squad_id;
        
        DROP INDEX IF EXISTS ix_challenge_submissions_squad_id;
        
        ALTER TABLE challenge_submissions 
        DROP COLUMN IF EXISTS squad_id;
    """)