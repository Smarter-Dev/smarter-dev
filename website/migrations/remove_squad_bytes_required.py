"""
Migration script to remove bytes_required from Squad model and add squad_join_bytes_required to BytesConfig model.
"""

import os
import sys
from sqlalchemy import create_engine, Column, Integer, MetaData, Table, text

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from website.database import engine

def run_migration():
    """
    Run the migration to update the database schema.
    """
    # Create a connection
    conn = engine.connect()

    try:
        # Start a transaction
        trans = conn.begin()

        # For SQLite, we need to check if columns exist differently
        try:
            # Check if bytes_required column exists in squads table
            result = conn.execute(text("PRAGMA table_info(squads)"))
            columns = result.fetchall()
            print(f"Columns in squads table: {columns}")
            bytes_required_exists = any(col[1] == 'bytes_required' for col in columns)

            if bytes_required_exists:
                # For SQLite, we need to recreate the table to remove a column
                # First, get all column names except bytes_required
                column_names = [col[1] for col in columns if col[1] != 'bytes_required']
                column_list = ', '.join(column_names)

                # Create a new table without the bytes_required column
                # We need to include the column types and constraints
                create_table_sql = """
                CREATE TABLE squads_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    role_id BIGINT NOT NULL,
                    name VARCHAR NOT NULL,
                    description TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(guild_id) REFERENCES guilds(id)
                )
                """
                print(f"Creating new table with SQL: {create_table_sql}")
                conn.execute(text(create_table_sql))

                # Copy data from old table to new table
                copy_sql = f"INSERT INTO squads_new SELECT {column_list} FROM squads"
                print(f"Copying data with SQL: {copy_sql}")
                conn.execute(text(copy_sql))

                # Drop old table and rename new table
                print("Dropping old table and renaming new table")
                conn.execute(text("DROP TABLE squads"))
                conn.execute(text("ALTER TABLE squads_new RENAME TO squads"))

                print("Removed bytes_required column from squads table")
            else:
                print("bytes_required column does not exist in squads table")

            # Check if squad_join_bytes_required column exists in bytes_config table
            result = conn.execute(text("PRAGMA table_info(bytes_config)"))
            columns = result.fetchall()
            squad_join_bytes_required_exists = any(col[1] == 'squad_join_bytes_required' for col in columns)

            if not squad_join_bytes_required_exists:
                # Add squad_join_bytes_required column to bytes_config table
                conn.execute(text(
                    "ALTER TABLE bytes_config ADD COLUMN squad_join_bytes_required INTEGER NOT NULL DEFAULT 100"
                ))
                print("Added squad_join_bytes_required column to bytes_config table")
            else:
                print("squad_join_bytes_required column already exists in bytes_config table")

        except Exception as e:
            print(f"Error checking or modifying columns: {e}")
            raise

        # Commit the transaction
        trans.commit()
        print("Migration completed successfully")

    except Exception as e:
        # Rollback the transaction in case of error
        trans.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        # Close the connection
        conn.close()

if __name__ == "__main__":
    run_migration()
