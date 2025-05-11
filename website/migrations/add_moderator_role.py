"""
Migration script to add moderator_role_id column to the guilds table.
"""

import sqlite3
import os
import sys

# Add the project root to the path so we can import the database module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from website.database import SQLALCHEMY_DATABASE_URL

def run_migration():
    """
    Run the migration to add the moderator_role_id column to the guilds table.
    """
    # Extract the database path from the URL
    db_path = SQLALCHEMY_DATABASE_URL.replace('sqlite:///', '')

    print(f"Running migration on database: {db_path}")

    # Connect to the database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check if the column already exists
        cursor.execute("PRAGMA table_info(guilds)")
        columns = cursor.fetchall()
        column_names = [col[1] for col in columns]

        if 'moderator_role_id' not in column_names:
            print("Adding moderator_role_id column to guilds table...")
            cursor.execute("ALTER TABLE guilds ADD COLUMN moderator_role_id BIGINT")
            conn.commit()
            print("Migration completed successfully.")
        else:
            print("Column moderator_role_id already exists in guilds table. No migration needed.")

    except Exception as e:
        print(f"Error during migration: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    run_migration()
