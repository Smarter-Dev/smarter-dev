"""Fix challenge_inputs table to use standard timestamp columns

Revision ID: f7ca13df2ecc
Revises: 9b182de5f3c5
Create Date: 2025-08-14 17:17:46.199022

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7ca13df2ecc'
down_revision = '9b182de5f3c5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the standard created_at and updated_at columns
    op.execute('''
        ALTER TABLE challenge_inputs 
        ADD COLUMN created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    ''')
    
    # Create new index
    op.execute('CREATE INDEX ix_challenge_inputs_created_at ON challenge_inputs (created_at)')
    
    # Drop the old index if it exists
    op.execute('DROP INDEX IF EXISTS ix_challenge_inputs_generated_at')
    
    # Drop the existing generated_at column
    op.execute('ALTER TABLE challenge_inputs DROP COLUMN IF EXISTS generated_at')


def downgrade() -> None:
    # Reverse the changes - add back generated_at and remove standard columns
    op.execute('ALTER TABLE challenge_inputs ADD COLUMN generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()')
    op.execute('CREATE INDEX ix_challenge_inputs_generated_at ON challenge_inputs (generated_at)')
    
    # Drop new columns and index
    op.execute('DROP INDEX IF EXISTS ix_challenge_inputs_created_at')
    op.execute('ALTER TABLE challenge_inputs DROP COLUMN IF EXISTS updated_at')
    op.execute('ALTER TABLE challenge_inputs DROP COLUMN IF EXISTS created_at')