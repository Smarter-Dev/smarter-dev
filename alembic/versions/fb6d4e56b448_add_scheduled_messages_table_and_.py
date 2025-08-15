"""Add scheduled messages table and campaign fields

Revision ID: fb6d4e56b448
Revises: f7ca13df2ecc
Create Date: 2025-08-15 19:21:10.836491

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'fb6d4e56b448'
down_revision: Union[str, None] = 'f7ca13df2ecc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create scheduled_messages table
    op.create_table('scheduled_messages',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('campaign_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('scheduled_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_sent', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scheduled_messages_campaign_id'), 'scheduled_messages', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_scheduled_messages_scheduled_time'), 'scheduled_messages', ['scheduled_time'], unique=False)
    op.create_index(op.f('ix_scheduled_messages_is_sent'), 'scheduled_messages', ['is_sent'], unique=False)

    # Add missing columns to campaigns table if they don't exist
    try:
        op.add_column('campaigns', sa.Column('title', sa.String(), nullable=True))
        op.add_column('campaigns', sa.Column('description', sa.Text(), nullable=True))
    except:
        pass  # Columns might already exist

    # Add missing column to challenges table if it doesn't exist
    try:
        op.add_column('challenges', sa.Column('points_value', sa.Integer(), nullable=False, server_default='100'))
    except:
        pass  # Column might already exist


def downgrade() -> None:
    # Drop the scheduled_messages table
    op.drop_index(op.f('ix_scheduled_messages_is_sent'), table_name='scheduled_messages')
    op.drop_index(op.f('ix_scheduled_messages_scheduled_time'), table_name='scheduled_messages')
    op.drop_index(op.f('ix_scheduled_messages_campaign_id'), table_name='scheduled_messages')
    op.drop_table('scheduled_messages')
    
    # Remove added columns
    try:
        op.drop_column('campaigns', 'description')
        op.drop_column('campaigns', 'title')
    except:
        pass
        
    try:
        op.drop_column('challenges', 'points_value')
    except:
        pass