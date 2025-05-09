"""
Test the squad admin list route.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from website.models import Base, Squad, Guild
from website.database import get_db, engine
from website.app import app


def test_admin_squad_list():
    """Test listing squads through the admin interface."""
    # Create a test client
    client = TestClient(app)
    
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
        
        # Create a squad
        test_squad = Squad(
            guild_id=test_guild.id,
            role_id=123456789,
            name="Test Squad",
            description="A test squad",
            is_active=True
        )
        session.add(test_squad)
        session.commit()
        
        # Log in as admin
        client.post("/admin/login", data={"username": "admin", "password": "admin"})
        
        # Get the squads list
        response = client.get("/admin/discord/squads")
        
        # Check that the response is successful
        assert response.status_code == 200
        
    finally:
        # Clean up
        session.query(Squad).filter(Squad.id == test_squad.id).delete()
        session.commit()
        session.close()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
