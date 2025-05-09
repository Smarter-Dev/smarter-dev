"""
Migration script to move last_daily_bytes from discord_users to guild_members.

This script adds the last_daily_bytes column to the guild_members table
and moves the data from the discord_users table to the guild_members table.
"""

import sys
import os
from datetime import datetime
from sqlalchemy import create_engine, inspect, text

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Create the engine
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./smarter_dev.db")
engine = create_engine(DATABASE_URL)

def run_migration():
    """Run the migration"""
    print("Starting migration to move last_daily_bytes from discord_users to guild_members...")

    # Check if the column already exists in guild_members
    inspector = inspect(engine)
    guild_members_columns = [col["name"] for col in inspector.get_columns("guild_members")]

    with engine.connect() as conn:
        # Add last_daily_bytes column to guild_members if it doesn't exist
        if "last_daily_bytes" not in guild_members_columns:
            print("Adding last_daily_bytes column to guild_members table...")
            conn.execute(text("ALTER TABLE guild_members ADD COLUMN last_daily_bytes TIMESTAMP"))
        else:
            print("last_daily_bytes column already exists in guild_members, skipping...")

        # Move data from discord_users to guild_members
        print("Moving last_daily_bytes data from discord_users to guild_members...")
        
        # Get all users with last_daily_bytes
        result = conn.execute(text("""
            SELECT discord_id, last_daily_bytes 
            FROM discord_users 
            WHERE last_daily_bytes IS NOT NULL
        """))
        
        users_with_daily_bytes = result.fetchall()
        print(f"Found {len(users_with_daily_bytes)} users with last_daily_bytes")
        
        # For each user, update all their guild memberships
        for user in users_with_daily_bytes:
            discord_id = user[0]
            last_daily_bytes = user[1]
            
            # Get the user's internal ID
            user_result = conn.execute(text(f"""
                SELECT id FROM discord_users WHERE discord_id = {discord_id}
            """))
            user_id = user_result.fetchone()[0]
            
            # Update all guild memberships for this user
            conn.execute(text(f"""
                UPDATE guild_members 
                SET last_daily_bytes = '{last_daily_bytes}'
                WHERE user_id = {user_id}
            """))
            
            print(f"Updated guild memberships for user {discord_id}")

    print("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()