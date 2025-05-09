"""
Add streak tracking fields to guild_members table.
"""

from alembic import op
import sqlalchemy as sa


def upgrade():
    """Add streak tracking columns to guild_members table."""
    # Add streak tracking columns to guild_members table
    op.add_column('guild_members', sa.Column('last_active_day', sa.String(), nullable=True))
    op.add_column('guild_members', sa.Column('streak_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('guild_members', sa.Column('last_daily_bytes', sa.DateTime(), nullable=True))


def downgrade():
    """Remove streak tracking columns from guild_members table."""
    # Remove streak tracking columns from guild_members table
    op.drop_column('guild_members', 'last_active_day')
    op.drop_column('guild_members', 'streak_count')
    op.drop_column('guild_members', 'last_daily_bytes')
