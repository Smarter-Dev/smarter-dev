"""
Test the squad admin routes.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from website.models import Base, Squad, Guild
from website.database import get_db, engine
from website.app import app


def test_admin_squad_create():
    """Test creating a squad through the admin interface."""
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

        # Log in as admin
        client.post("/admin/login", data={"username": "admin", "password": "admin"})

        # Create a squad
        response = client.post(
            "/admin/discord/squads/new",
            data={
                "guild_id": test_guild.id,
                "role_id": 123456789,
                "name": "Test Squad",
                "description": "A test squad",
                "is_active": "on"
            },
            follow_redirects=False
        )

        # Check that we were redirected to the squads list
        assert response.status_code == 302
        assert response.headers["location"] == "/admin/discord/squads"

        # Check that the squad was created
        squad = session.query(Squad).filter(Squad.name == "Test Squad").first()
        assert squad is not None
        assert squad.name == "Test Squad"
        assert squad.role_id == 123456789
        assert squad.guild_id == test_guild.id

    finally:
        # Clean up
        session.query(Squad).filter(Squad.name == "Test Squad").delete()
        session.commit()
        session.close()


def test_admin_squad_edit():
    """Test editing a squad through the admin interface."""
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

        # Edit the squad
        response = client.post(
            f"/admin/discord/squads/{test_squad.id}/edit",
            data={
                "guild_id": test_guild.id,
                "role_id": 987654321,
                "name": "Updated Squad",
                "description": "An updated test squad",
                "is_active": "on"
            },
            follow_redirects=False
        )

        # Check that we were redirected to the squads list
        assert response.status_code == 302
        assert response.headers["location"] == "/admin/discord/squads"

        # Check that the squad was updated
        session.refresh(test_squad)
        assert test_squad.name == "Updated Squad"
        assert test_squad.role_id == 987654321
        assert test_squad.description == "An updated test squad"

    finally:
        # Clean up
        session.query(Squad).filter(Squad.id == test_squad.id).delete()
        session.commit()
        session.close()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
