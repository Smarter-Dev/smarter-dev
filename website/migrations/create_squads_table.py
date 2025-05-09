"""
Migration script to create the squads and squad_members tables.
"""

import os
import sys
from sqlalchemy import create_engine, Column, Integer, MetaData, Table, text

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from website.database import engine
from website.models import Base, Squad, SquadMember

def run_migration():
    """
    Run the migration to create the squads and squad_members tables.
    """
    # Create the tables
    Base.metadata.create_all(engine)
    
    print("Created squads and squad_members tables")

if __name__ == "__main__":
    run_migration()
