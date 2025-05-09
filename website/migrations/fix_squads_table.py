"""
Migration script to fix the squads table by recreating it with the correct primary key.
"""

import os
import sys
import sqlite3

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def run_migration():
    """
    Run the migration to fix the squads table.
    """
    # Connect to the database
    conn = sqlite3.connect('smarter_dev.db')
    cursor = conn.cursor()

    try:
        # Start a transaction
        conn.execute("BEGIN TRANSACTION")

        # Drop the existing tables
        cursor.execute("DROP TABLE IF EXISTS squad_members")
        cursor.execute("DROP TABLE IF EXISTS squads")

        # Create the squads table with the correct primary key
        cursor.execute("""
        CREATE TABLE squads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(guild_id) REFERENCES guilds(id)
        )
        """)

        # Create an index on the guild_id column
        cursor.execute("CREATE INDEX ix_squads_guild_id ON squads (guild_id)")

        # Create an index on the id column
        cursor.execute("CREATE INDEX ix_squads_id ON squads (id)")

        # Create the squad_members table
        cursor.execute("""
        CREATE TABLE squad_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            squad_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(squad_id) REFERENCES squads(id),
            FOREIGN KEY(user_id) REFERENCES discord_users(id)
        )
        """)

        # Create indexes on the squad_members table
        cursor.execute("CREATE INDEX ix_squad_members_squad_id ON squad_members (squad_id)")
        cursor.execute("CREATE INDEX ix_squad_members_user_id ON squad_members (user_id)")

        # Commit the transaction
        conn.execute("COMMIT")

        print("Fixed squads and squad_members tables")

    except Exception as e:
        # Rollback the transaction in case of error
        conn.execute("ROLLBACK")
        print(f"Migration failed: {e}")
        raise
    finally:
        # Close the connection
        conn.close()

if __name__ == "__main__":
    run_migration()
