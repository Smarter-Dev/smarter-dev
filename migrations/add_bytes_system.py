"""
Migration script to add the Bytes system to the database.

This script:
1. Adds the bytes_balance column to the DiscordUser model
2. Creates the bytes, bytes_config, bytes_roles, and bytes_cooldowns tables
"""

import sys
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, DateTime, ForeignKey, func
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
    from sqlalchemy import inspect
    inspector = inspect(engine)
    columns = [col["name"] for col in inspector.get_columns("discord_users")]

    if "bytes_balance" not in columns:
        print("Adding bytes_balance column to DiscordUser model...")
        # Add the bytes_balance column
        with engine.connect() as conn:
            conn.execute("ALTER TABLE discord_users ADD COLUMN bytes_balance INTEGER DEFAULT 0 NOT NULL")

            # Update all users to have a starting balance of 100
            conn.execute("UPDATE discord_users SET bytes_balance = 100")
    else:
        print("bytes_balance column already exists, skipping...")

    # Create the new tables
    # Check if the bytes table already exists
    tables = inspector.get_table_names()

    with engine.connect() as conn:
        if "bytes" not in tables:
            print("Creating bytes table...")
            # Create the bytes table
            conn.execute("""
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
            """)
        else:
            print("bytes table already exists, skipping...")

        if "bytes_config" not in tables:
            print("Creating bytes_config table...")
            # Create the bytes_config table
            conn.execute("""
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
            """)
        else:
            print("bytes_config table already exists, skipping...")

        if "bytes_roles" not in tables:
            print("Creating bytes_roles table...")
            # Create the bytes_roles table
            conn.execute("""
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
            """)
        else:
            print("bytes_roles table already exists, skipping...")

        if "bytes_cooldowns" not in tables:
            print("Creating bytes_cooldowns table...")
            # Create the bytes_cooldowns table
            conn.execute("""
            CREATE TABLE bytes_cooldowns (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                last_given_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES discord_users (id),
                FOREIGN KEY (guild_id) REFERENCES guilds (id)
            )
            """)
        else:
            print("bytes_cooldowns table already exists, skipping...")

    # Convert existing kudos to bytes
    print("Converting existing kudos to bytes...")
    kudos = session.query(Kudos).all()

    for k in kudos:
        # Check if the bytes entry already exists
        existing = engine.execute(f"""
        SELECT id FROM bytes
        WHERE giver_id = {k.giver_id}
        AND receiver_id = {k.receiver_id}
        AND guild_id = {k.guild_id}
        AND awarded_at = '{k.awarded_at}'
        """).fetchone()

        if not existing:
            # Add a bytes entry
            engine.execute(f"""
            INSERT INTO bytes (giver_id, receiver_id, guild_id, amount, reason, awarded_at)
            VALUES ({k.giver_id}, {k.receiver_id}, {k.guild_id}, {k.amount}, '{k.reason.replace("'", "''")}', '{k.awarded_at}')
            """)

            # Update the user's bytes balance
            engine.execute(f"""
            UPDATE discord_users SET bytes_balance = bytes_balance + {k.amount}
            WHERE id = {k.receiver_id}
            """)

    print("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
