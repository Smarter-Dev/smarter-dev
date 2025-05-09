"""
Migration script to add streak tracking to the database.

This script adds the following columns to the discord_users table:
1. last_active_day: The last day the user was active (in YYYY-MM-DD format)
2. streak_count: The number of consecutive days the user has been active
3. last_daily_bytes: The timestamp when the user last received daily bytes
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
    print("Starting migration to add streak tracking...")

    # Check if the columns already exist
    inspector = inspect(engine)
    columns = [col["name"] for col in inspector.get_columns("discord_users")]

    with engine.connect() as conn:
        # Add last_active_day column
        if "last_active_day" not in columns:
            print("Adding last_active_day column to discord_users table...")
            conn.execute(text("ALTER TABLE discord_users ADD COLUMN last_active_day VARCHAR"))
        else:
            print("last_active_day column already exists, skipping...")

        # Add streak_count column
        if "streak_count" not in columns:
            print("Adding streak_count column to discord_users table...")
            conn.execute(text("ALTER TABLE discord_users ADD COLUMN streak_count INTEGER DEFAULT 0 NOT NULL"))
        else:
            print("streak_count column already exists, skipping...")

        # Add last_daily_bytes column
        if "last_daily_bytes" not in columns:
            print("Adding last_daily_bytes column to discord_users table...")
            conn.execute(text("ALTER TABLE discord_users ADD COLUMN last_daily_bytes TIMESTAMP"))
        else:
            print("last_daily_bytes column already exists, skipping...")

    print("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
