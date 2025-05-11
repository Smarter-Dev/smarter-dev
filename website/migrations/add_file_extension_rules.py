"""
Migration to add default allowed file extensions.
"""

import os
import sys
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add the website directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from website.models import Base, Guild, AutoModFileExtensionRule

# Default allowed extensions (common image and video formats)
DEFAULT_ALLOWED_EXTENSIONS = [
    # Images
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg",
    # Videos
    "mp4", "webm", "mov", "avi", "mkv", "flv", "wmv",
]

# Use absolute path for the database
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'smarter_dev.db'))

def run_migration():
    """
    Add default allowed file extensions for all existing guilds.
    """
    # Create engine and session
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Get all guilds
        guilds = session.query(Guild).all()

        # Add default extensions for each guild
        for guild in guilds:
            for extension in DEFAULT_ALLOWED_EXTENSIONS:
                # Check if rule already exists
                existing = session.query(AutoModFileExtensionRule).filter_by(
                    guild_id=guild.id,
                    extension=extension
                ).first()

                if not existing:
                    # Create new rule
                    rule = AutoModFileExtensionRule(
                        guild_id=guild.id,
                        extension=extension,
                        is_allowed=True,
                        warning_message=None
                    )
                    session.add(rule)

        # Commit changes
        session.commit()
        print("Successfully added default file extension rules")

    except Exception as e:
        print(f"Error adding default file extension rules: {e}")
        session.rollback()
        raise

    finally:
        session.close()

if __name__ == "__main__":
    run_migration() 