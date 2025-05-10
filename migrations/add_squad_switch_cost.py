"""
Migration script to add squad_switch_cost to the bytes_config table.

This script adds the following column to the bytes_config table:
1. squad_switch_cost: The cost in bytes to switch squads (default: 50)
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
    print("Starting migration to add squad_switch_cost...")

    # Check if the column already exists
    inspector = inspect(engine)
    columns = [col["name"] for col in inspector.get_columns("bytes_config")]

    with engine.connect() as conn:
        # Add squad_switch_cost column
        if "squad_switch_cost" not in columns:
            print("Adding squad_switch_cost column to bytes_config table...")
            conn.execute(text("ALTER TABLE bytes_config ADD COLUMN squad_switch_cost INTEGER DEFAULT 50 NOT NULL"))
        else:
            print("squad_switch_cost column already exists, skipping...")

    print("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()