"""
Test the Squad model directly.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from website.models import Base, Squad, Guild
from website.database import get_db, engine


def test_squad_model():
    """Test creating a Squad directly in the database."""
    # Create a session
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Check if guild already exists
        test_guild = session.query(Guild).filter(Guild.discord_id == 987654321).first()
        if not test_guild:
            test_guild = Guild(
                discord_id=987654321,
                name="test_guild"
            )
            session.add(test_guild)
            session.commit()

        # Create a test squad
        test_squad = Squad(
            guild_id=test_guild.id,
            role_id=123456789,
            name="Test Squad",
            description="A test squad",
            is_active=True
        )
        session.add(test_squad)
        session.commit()

        # Verify the squad was created
        squad = session.query(Squad).filter(Squad.name == "Test Squad").first()
        assert squad is not None
        assert squad.name == "Test Squad"
        assert squad.role_id == 123456789
        assert squad.guild_id == test_guild.id

    finally:
        # Clean up
        session.rollback()  # Roll back any failed transactions
        session.query(Squad).filter(Squad.guild_id == test_guild.id).delete()
        session.commit()
        session.close()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
