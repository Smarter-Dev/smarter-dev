"""
Migration script to add the Bytes system to the database.

This script:
1. Adds the bytes_balance column to the DiscordUser model
2. Creates the bytes, bytes_config, bytes_roles, and bytes_cooldowns tables
"""

import sys
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, DateTime, ForeignKey, func, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from website.models import Base, DiscordUser, Guild, Kudos

# Create the engine
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./smarter_dev.db")
engine = create_engine(DATABASE_URL)

# Create a session
Session = sessionmaker(bind=engine)
session = Session()

def run_migration():
    """Run the migration"""
    print("Starting migration to add Bytes system...")

    # Check if the bytes_balance column already exists
    inspector = inspect(engine)
    columns = [col["name"] for col in inspector.get_columns("discord_users")]

    with engine.connect() as conn:
        if "bytes_balance" not in columns:
            print("Adding bytes_balance column to DiscordUser model...")
            # Add the bytes_balance column
            conn.execute(text("ALTER TABLE discord_users ADD COLUMN bytes_balance INTEGER DEFAULT 0 NOT NULL"))

            # Update all users to have a starting balance of 100
            conn.execute(text("UPDATE discord_users SET bytes_balance = 100"))
        else:
            print("bytes_balance column already exists, skipping...")

        # Create the new tables
        # Check if the bytes table already exists
        tables = inspector.get_table_names()

        if "bytes" not in tables:
            print("Creating bytes table...")
            # Create the bytes table
            conn.execute(text("""
            CREATE TABLE bytes (
                id INTEGER PRIMARY KEY,
                giver_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                amount INTEGER DEFAULT 1 NOT NULL,
                reason TEXT,
                awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (giver_id) REFERENCES discord_users (id),
                FOREIGN KEY (receiver_id) REFERENCES discord_users (id),
                FOREIGN KEY (guild_id) REFERENCES guilds (id)
            )
            """))
        else:
            print("bytes table already exists, skipping...")

        if "bytes_config" not in tables:
            print("Creating bytes_config table...")
            # Create the bytes_config table
            conn.execute(text("""
            CREATE TABLE bytes_config (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                starting_balance INTEGER DEFAULT 100 NOT NULL,
                daily_earning INTEGER DEFAULT 10 NOT NULL,
                max_give_amount INTEGER DEFAULT 50 NOT NULL,
                cooldown_minutes INTEGER DEFAULT 1440 NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (guild_id) REFERENCES guilds (id),
                UNIQUE (guild_id)
            )
            """))
        else:
            print("bytes_config table already exists, skipping...")

        if "bytes_roles" not in tables:
            print("Creating bytes_roles table...")
            # Create the bytes_roles table
            conn.execute(text("""
            CREATE TABLE bytes_roles (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                role_id BIGINT NOT NULL,
                role_name VARCHAR NOT NULL,
                bytes_required INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (guild_id) REFERENCES guilds (id)
            )
            """))
        else:
            print("bytes_roles table already exists, skipping...")

        if "bytes_cooldowns" not in tables:
            print("Creating bytes_cooldowns table...")
            # Create the bytes_cooldowns table
            conn.execute(text("""
            CREATE TABLE bytes_cooldowns (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                last_given_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES discord_users (id),
                FOREIGN KEY (guild_id) REFERENCES guilds (id)
            )
            """))
        else:
            print("bytes_cooldowns table already exists, skipping...")

    # We'll skip converting existing kudos to bytes for now
    print("Skipping conversion of existing kudos to bytes...")

    print("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
