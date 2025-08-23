"""Add challenge_inputs table for squad-specific challenge inputs

Revision ID: 9b182de5f3c5
Revises: 7b22e1920c66
Create Date: 2025-08-14 17:00:36.354256

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '9b182de5f3c5'
down_revision = '7b22e1920c66'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create challenge_inputs table
    op.create_table(
        'challenge_inputs',
        sa.Column('challenge_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('squad_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('input_data', sa.Text(), nullable=False),
        sa.Column('result_data', sa.Text(), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['challenge_id'], ['challenges.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['squad_id'], ['squads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('challenge_id', 'squad_id')
    )
    
    # Create indexes for efficient querying
    op.create_index('ix_challenge_inputs_challenge_id', 'challenge_inputs', ['challenge_id'])
    op.create_index('ix_challenge_inputs_squad_id', 'challenge_inputs', ['squad_id'])
    op.create_index('ix_challenge_inputs_generated_at', 'challenge_inputs', ['generated_at'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_challenge_inputs_generated_at', table_name='challenge_inputs')
    op.drop_index('ix_challenge_inputs_squad_id', table_name='challenge_inputs')
    op.drop_index('ix_challenge_inputs_challenge_id', table_name='challenge_inputs')
    
    # Drop table
    op.drop_table('challenge_inputs')