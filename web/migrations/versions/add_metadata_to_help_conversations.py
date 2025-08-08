"""Add command_metadata field to help_conversations

Revision ID: g2a3b4c5d6e7
Revises: f1a2b3c4d5e6
Create Date: 2025-01-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = 'g2a3b4c5d6e7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    """Add command_metadata column to help_conversations table."""
    # Add command_metadata column as nullable JSON field
    op.add_column('help_conversations', 
                  sa.Column('command_metadata', postgresql.JSON(astext_type=sa.Text()), nullable=True))


def downgrade():
    """Remove command_metadata column from help_conversations table."""
    # Drop command_metadata column
    op.drop_column('help_conversations', 'command_metadata')