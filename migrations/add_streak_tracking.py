from sqlalchemy import Column, Integer, DateTime, BigInteger, func
from alembic import op
import sqlalchemy as sa
from datetime import datetime

# Revision identifiers
revision = 'add_streak_tracking'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Add streak tracking columns to discord_users table
    op.add_column('discord_users', sa.Column('last_active_day', sa.String(), nullable=True))
    op.add_column('discord_users', sa.Column('streak_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('discord_users', sa.Column('last_daily_bytes', sa.DateTime(), nullable=True))

def downgrade():
    # Remove streak tracking columns from discord_users table
    op.drop_column('discord_users', 'last_active_day')
    op.drop_column('discord_users', 'streak_count')
    op.drop_column('discord_users', 'last_daily_bytes')