"""Add SquadActivity model for activity tracking

Revision ID: f1a2b3c4d5e6
Revises: e11a3e10ff8b
Create Date: 2024-07-02 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = 'f1a2b3c4d5e6'
down_revision = 'e11a3e10ff8b'
branch_labels = None
depends_on = None


def upgrade():
    """Create squad_activities table with proper indexes and constraints."""
    # Create squad_activities table
    op.create_table(
        'squad_activities',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('guild_id', sa.String(length=20), nullable=False),
        sa.Column('user_id', sa.String(length=20), nullable=False),
        sa.Column('squad_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('activity_type', sa.String(length=100), nullable=False),
        sa.Column('metadata', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        
        # Foreign key constraints
        sa.ForeignKeyConstraint(['squad_id'], ['squads.id'], ),
        
        # Check constraints for data validation
        sa.CheckConstraint('length(activity_type) >= 1', name='ck_activity_type_not_empty'),
        sa.CheckConstraint('length(guild_id) >= 10', name='ck_guild_id_valid_format'),
        sa.CheckConstraint('length(user_id) >= 10', name='ck_user_id_valid_format'),
        
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for performance optimization
    # Single column indexes
    op.create_index('ix_squad_activity_guild_created', 'squad_activities', ['guild_id', 'created_at'])
    op.create_index('ix_squad_activity_user_created', 'squad_activities', ['user_id', 'created_at'])
    op.create_index('ix_squad_activity_squad_created', 'squad_activities', ['squad_id', 'created_at'])
    op.create_index('ix_squad_activity_type_created', 'squad_activities', ['activity_type', 'created_at'])
    
    # Composite indexes for common query patterns
    op.create_index('ix_squad_activity_guild_type', 'squad_activities', ['guild_id', 'activity_type'])
    op.create_index('ix_squad_activity_user_type', 'squad_activities', ['user_id', 'activity_type'])


def downgrade():
    """Drop squad_activities table and all related indexes."""
    # Drop indexes first
    op.drop_index('ix_squad_activity_user_type', table_name='squad_activities')
    op.drop_index('ix_squad_activity_guild_type', table_name='squad_activities')
    op.drop_index('ix_squad_activity_type_created', table_name='squad_activities')
    op.drop_index('ix_squad_activity_squad_created', table_name='squad_activities')
    op.drop_index('ix_squad_activity_user_created', table_name='squad_activities')
    op.drop_index('ix_squad_activity_guild_created', table_name='squad_activities')
    
    # Drop table
    op.drop_table('squad_activities')