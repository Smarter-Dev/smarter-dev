"""
Test that users can only be in a single squad at a time.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from website.models import Base, Squad, Guild, DiscordUser, SquadMember, BytesConfig
from website.database import get_db, engine
from website.app import app


@pytest.mark.asyncio
async def test_user_single_squad_membership():
    """Test that a user can only be in one squad at a time."""
    # Create a test client
    client = TestClient(app)

    # Create a session
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Create test guild
        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        session.add(test_guild)
        session.commit()

        # Create bytes config
        bytes_config = BytesConfig(
            guild_id=test_guild.id,
            squad_join_bytes_required=0  # No bytes required for testing
        )
        session.add(bytes_config)
        session.commit()

        # Create test user
        test_user = DiscordUser(
            discord_id=123456789,
            username="test_user",
            bytes_balance=100
        )
        session.add(test_user)
        session.commit()

        # Create two squads
        squad1 = Squad(
            guild_id=test_guild.id,
            role_id=111111111,
            name="Squad 1",
            description="First test squad"
        )
        session.add(squad1)

        squad2 = Squad(
            guild_id=test_guild.id,
            role_id=222222222,
            name="Squad 2",
            description="Second test squad"
        )
        session.add(squad2)
        session.commit()

        # Get API token
        response = client.post(
            "/api/auth/token",
            json={"api_key": "TESTING"}
        )
        assert response.status_code == 200
        api_token = response.json()["access_token"]

        # Add user to first squad
        headers = {"Authorization": f"Bearer {api_token}"}
        response = client.post(
            f"/api/squads/{squad1.id}/members",
            json={"user_id": test_user.discord_id},
            headers=headers
        )
        assert response.status_code == 201

        # Verify user is in first squad
        response = client.get(
            f"/api/users/{test_user.discord_id}/squads",
            headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["squads"]) == 1
        assert data["squads"][0]["id"] == squad1.id

        # Add user to second squad
        response = client.post(
            f"/api/squads/{squad2.id}/members",
            json={"user_id": test_user.discord_id},
            headers=headers
        )
        assert response.status_code == 201

        # Verify user is now only in second squad
        response = client.get(
            f"/api/users/{test_user.discord_id}/squads",
            headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["squads"]) == 1
        assert data["squads"][0]["id"] == squad2.id

        # Verify user is no longer in first squad
        squad_members = session.query(SquadMember).filter(
            SquadMember.user_id == test_user.id,
            SquadMember.squad_id == squad1.id
        ).all()
        assert len(squad_members) == 0

    finally:
        # Clean up
        session.query(SquadMember).delete()
        session.query(Squad).delete()
        session.query(BytesConfig).delete()
        session.query(DiscordUser).delete()
        session.query(Guild).delete()
        session.commit()
        session.close()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
