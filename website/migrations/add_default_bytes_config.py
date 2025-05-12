"""Add default bytes configuration to existing guilds

Revision ID: add_default_bytes_config
Revises: 
Create Date: 2024-03-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session
from datetime import datetime

# revision identifiers, used by Alembic
revision = 'add_default_bytes_config'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Get database connection
    connection = op.get_bind()
    session = Session(bind=connection)
    
    # Get all guilds that don't have a bytes config
    result = connection.execute("""
        SELECT g.id 
        FROM guilds g 
        LEFT JOIN bytes_config bc ON g.id = bc.guild_id 
        WHERE bc.id IS NULL
    """)
    
    guilds_without_config = [row[0] for row in result]
    
    # Add default config for each guild
    for guild_id in guilds_without_config:
        connection.execute(
            """
            INSERT INTO bytes_config (
                guild_id, 
                starting_balance, 
                daily_earning, 
                max_give_amount, 
                cooldown_minutes, 
                squad_join_bytes_required, 
                squad_switch_cost,
                created_at,
                updated_at
            ) VALUES (
                :guild_id,
                100,  -- starting_balance
                10,   -- daily_earning
                50,   -- max_give_amount
                1440, -- cooldown_minutes (24 hours)
                100,  -- squad_join_bytes_required
                50,   -- squad_switch_cost
                :now,
                :now
            )
            """,
            {
                "guild_id": guild_id,
                "now": datetime.utcnow()
            }
        )

def downgrade():
    # No downgrade needed as this is adding default data
    pass 