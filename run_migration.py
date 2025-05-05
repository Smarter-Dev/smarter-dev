"""
Script to run database migrations.
"""

import os
import sys
import importlib.util

def run_migration(migration_file):
    """
    Run a migration script.
    
    Args:
        migration_file: Path to the migration script
    """
    print(f"Running migration: {migration_file}")
    
    # Import the migration module
    spec = importlib.util.spec_from_file_location("migration", migration_file)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    
    # Run the migration
    migration.run_migration()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_migration.py <migration_file>")
        sys.exit(1)
    
    migration_file = sys.argv[1]
    
    if not os.path.exists(migration_file):
        print(f"Migration file not found: {migration_file}")
        sys.exit(1)
    
    run_migration(migration_file)
