"""Make all extra challenge columns nullable

Revision ID: 3e239ae837e4
Revises: 65cdcd7d431e
Create Date: 2025-08-16 07:23:58.534576

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3e239ae837e4'
down_revision = '65cdcd7d431e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make all extra columns nullable that don't exist in the model
    # These columns seem to have been added by previous migrations but aren't in the model
    
    columns_to_make_nullable = [
        'categories',
        'script_updated_at',
        'generation_script'
    ]
    
    for column in columns_to_make_nullable:
        op.execute(f"""
            DO $$ 
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'challenges' 
                    AND column_name = '{column}'
                ) THEN
                    ALTER TABLE challenges 
                    ALTER COLUMN {column} DROP NOT NULL;
                END IF;
            END $$;
        """)


def downgrade() -> None:
    # Restore NOT NULL constraints
    columns_to_restore = [
        'categories',
        'script_updated_at', 
        'generation_script'
    ]
    
    for column in columns_to_restore:
        op.execute(f"""
            DO $$ 
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'challenges' 
                    AND column_name = '{column}'
                ) THEN
                    ALTER TABLE challenges 
                    ALTER COLUMN {column} SET NOT NULL;
                END IF;
            END $$;
        """)