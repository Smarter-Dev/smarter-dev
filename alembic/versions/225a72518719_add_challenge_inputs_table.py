"""Add challenge_inputs table

Revision ID: 225a72518719
Revises: 3e239ae837e4
Create Date: 2025-08-16 07:31:35.160590

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '225a72518719'
down_revision = '3e239ae837e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create challenge_inputs table if it doesn't exist
    op.execute("""
        CREATE TABLE IF NOT EXISTS challenge_inputs (
            challenge_id UUID NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
            squad_id UUID NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
            input_data TEXT NOT NULL,
            result_data TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (challenge_id, squad_id)
        );
    """)
    
    # Create indexes
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_challenge_inputs_challenge_id 
        ON challenge_inputs (challenge_id);
    """)
    
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_challenge_inputs_squad_id 
        ON challenge_inputs (squad_id);
    """)


def downgrade() -> None:
    # Drop the table if it exists
    op.execute("DROP TABLE IF EXISTS challenge_inputs;")