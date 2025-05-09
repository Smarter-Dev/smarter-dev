"""
Integration tests for the squad system API endpoints.

This test suite verifies that the squad system API endpoints work correctly.
"""

import pytest
import os
import sys
import json
from datetime import datetime
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
import httpx
from starlette.testclient import TestClient

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from website.models import Base, DiscordUser, Guild, Squad, SquadMember, Bytes
from website.database import get_db, engine
from website.api_auth import create_jwt_token
from website.app import app


@pytest.fixture
def test_db():
    """Set up a test database session."""
    # Create a session
    Session = sessionmaker(bind=engine)
    session = Session()

    # Set up test data
    yield session

    # Clean up test data
    session.close()


@pytest.fixture
def async_client():
    """Create an async client for testing."""
    # Set the environment variable for local development
    os.environ["SMARTER_DEV_LOCAL"] = "1"

    # Create an async client
    from starlette.testclient import TestClient
    from starlette.routing import Route
    from starlette.applications import Starlette
    from website.api_routes import (
        squad_list, squad_detail, squad_create, squad_update, squad_delete,
        squad_member_list, squad_member_add, squad_member_remove,
        user_squads, user_eligible_squads
    )

    # Create a test app with the API routes we need
    test_app = Starlette(
        debug=True,
        routes=[
            Route("/api/squads", squad_list, methods=["GET"]),
            Route("/api/squads/{squad_id:int}", squad_detail, methods=["GET"]),
            Route("/api/squads", squad_create, methods=["POST"]),
            Route("/api/squads/{squad_id:int}", squad_update, methods=["PUT"]),
            Route("/api/squads/{squad_id:int}", squad_delete, methods=["DELETE"]),
            Route("/api/squads/{squad_id:int}/members", squad_member_list, methods=["GET"]),
            Route("/api/squads/{squad_id:int}/members", squad_member_add, methods=["POST"]),
            Route("/api/squads/{squad_id:int}/members/{user_id}", squad_member_remove, methods=["DELETE"]),
            Route("/api/users/{user_id}/squads", user_squads, methods=["GET"]),
            Route("/api/users/{user_id}/eligible-squads", user_eligible_squads, methods=["GET"]),
        ]
    )

    # Create a test client
    client = TestClient(test_app)

    # Return an async client that wraps the test client
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://testserver")


@pytest.fixture
def api_token():
    """Create a test API token."""
    # Generate a token for testing
    return create_jwt_token(999, "Testing API Key")


@pytest.mark.asyncio
async def test_squad_create_and_list(async_client, test_db, api_token):
    """Test creating a squad and listing squads."""
    # Use the client as a context manager to ensure it's closed properly
    async with async_client as client:
        # Create test data
        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        test_db.add(test_guild)
        test_db.commit()

        try:
            # Create a squad
            headers = {"Authorization": f"Bearer {api_token}"}
            response = await client.post(
                "/api/squads",
                json={
                    "guild_id": test_guild.id,
                    "role_id": 123456789,
                    "name": "Test Squad",
                    "description": "A test squad",
                    "bytes_required": 100,
                    "is_active": True
                },
                headers=headers
            )
            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "Test Squad"
            assert data["bytes_required"] == 100

            # List squads
            response = await client.get(
                f"/api/squads?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["squads"]) == 1
            assert data["squads"][0]["name"] == "Test Squad"

        finally:
            # Clean up test data
            test_db.query(Squad).filter(Squad.guild_id == test_guild.id).delete()
            test_db.query(Guild).filter(Guild.id == test_guild.id).delete()
            test_db.commit()


@pytest.mark.asyncio
async def test_squad_detail_and_update(async_client, test_db, api_token):
    """Test getting squad details and updating a squad."""
    # Use the client as a context manager to ensure it's closed properly
    async with async_client as client:
        # Create test data
        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        test_db.add(test_guild)
        test_db.commit()

        test_squad = Squad(
            guild_id=test_guild.id,
            role_id=123456789,
            name="Test Squad",
            description="A test squad",
            bytes_required=100,
            is_active=True
        )
        test_db.add(test_squad)
        test_db.commit()

        try:
            # Get squad details
            headers = {"Authorization": f"Bearer {api_token}"}
            response = await client.get(
                f"/api/squads/{test_squad.id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Test Squad"
            assert data["bytes_required"] == 100
            assert data["members_count"] == 0

            # Update squad
            response = await client.put(
                f"/api/squads/{test_squad.id}",
                json={
                    "name": "Updated Squad",
                    "bytes_required": 200
                },
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Updated Squad"
            assert data["bytes_required"] == 200

            # Verify update
            response = await client.get(
                f"/api/squads/{test_squad.id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Updated Squad"
            assert data["bytes_required"] == 200

        finally:
            # Clean up test data
            test_db.query(Squad).filter(Squad.id == test_squad.id).delete()
            test_db.query(Guild).filter(Guild.id == test_guild.id).delete()
            test_db.commit()


@pytest.mark.asyncio
async def test_squad_members(async_client, test_db, api_token):
    """Test adding and removing squad members."""
    # Use the client as a context manager to ensure it's closed properly
    async with async_client as client:
        # Create test data
        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        test_db.add(test_guild)
        test_db.commit()

        test_user = DiscordUser(
            discord_id=123456789,
            username="test_user",
            bytes_balance=200
        )
        test_db.add(test_user)
        test_db.commit()

        # Create a system user for giving bytes
        system_user = test_db.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
        if not system_user:
            system_user = DiscordUser(
                discord_id=0,
                username="System",
                bytes_balance=999999
            )
            test_db.add(system_user)
            test_db.commit()

        # Give bytes to the test user
        bytes_transaction = Bytes(
            giver_id=system_user.id,
            receiver_id=test_user.id,
            guild_id=test_guild.id,
            amount=200,
            reason="Test bytes"
        )
        test_db.add(bytes_transaction)
        test_db.commit()

        test_squad = Squad(
            guild_id=test_guild.id,
            role_id=123456789,
            name="Test Squad",
            description="A test squad",
            bytes_required=100,
            is_active=True
        )
        test_db.add(test_squad)
        test_db.commit()

        try:
            # Add user to squad
            headers = {"Authorization": f"Bearer {api_token}"}
            response = await client.post(
                f"/api/squads/{test_squad.id}/members",
                json={
                    "user_id": test_user.discord_id
                },
                headers=headers
            )
            assert response.status_code == 201
            data = response.json()
            assert data["squad_id"] == test_squad.id
            assert data["user_id"] == test_user.id

            # List squad members
            response = await client.get(
                f"/api/squads/{test_squad.id}/members",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["members"]) == 1
            assert data["members"][0]["user"]["discord_id"] == test_user.discord_id

            # Get user's squads
            response = await client.get(
                f"/api/users/{test_user.discord_id}/squads",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["squads"]) == 1
            assert data["squads"][0]["id"] == test_squad.id

            # Remove user from squad
            response = await client.delete(
                f"/api/squads/{test_squad.id}/members/{test_user.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True

            # Verify user was removed
            response = await client.get(
                f"/api/squads/{test_squad.id}/members",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["members"]) == 0

        finally:
            # Clean up test data
            test_db.query(SquadMember).filter(SquadMember.squad_id == test_squad.id).delete()
            test_db.query(Bytes).filter(Bytes.receiver_id == test_user.id).delete()
            test_db.query(Squad).filter(Squad.id == test_squad.id).delete()
            test_db.query(DiscordUser).filter(DiscordUser.id == test_user.id).delete()
            system_user = test_db.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
            if system_user:
                test_db.query(DiscordUser).filter(DiscordUser.id == system_user.id).delete()
            test_db.query(Guild).filter(Guild.id == test_guild.id).delete()
            test_db.commit()


@pytest.mark.asyncio
async def test_user_eligible_squads(async_client, test_db, api_token):
    """Test getting squads a user is eligible to join."""
    # Use the client as a context manager to ensure it's closed properly
    async with async_client as client:
        # Create test data
        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        test_db.add(test_guild)
        test_db.commit()

        test_user = DiscordUser(
            discord_id=123456789,
            username="test_user",
            bytes_balance=200
        )
        test_db.add(test_user)
        test_db.commit()

        # Create a system user for giving bytes
        system_user = test_db.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
        if not system_user:
            system_user = DiscordUser(
                discord_id=0,
                username="System",
                bytes_balance=999999
            )
            test_db.add(system_user)
            test_db.commit()

        # Give bytes to the test user
        bytes_transaction = Bytes(
            giver_id=system_user.id,
            receiver_id=test_user.id,
            guild_id=test_guild.id,
            amount=200,
            reason="Test bytes"
        )
        test_db.add(bytes_transaction)
        test_db.commit()

        # Create squads with different byte requirements
        squad1 = Squad(
            guild_id=test_guild.id,
            role_id=111222333,
            name="Bronze Squad",
            bytes_required=100,
            is_active=True
        )
        squad2 = Squad(
            guild_id=test_guild.id,
            role_id=444555666,
            name="Silver Squad",
            bytes_required=200,
            is_active=True
        )
        squad3 = Squad(
            guild_id=test_guild.id,
            role_id=777888999,
            name="Gold Squad",
            bytes_required=300,
            is_active=True
        )
        test_db.add_all([squad1, squad2, squad3])
        test_db.commit()

        try:
            # Get eligible squads
            headers = {"Authorization": f"Bearer {api_token}"}
            response = await client.get(
                f"/api/users/{test_user.discord_id}/eligible-squads?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["squads"]) == 2  # Should be eligible for Bronze and Silver
            assert data["bytes_received"] == 200  # Bytes received from the transaction

            # Add user to Bronze Squad
            response = await client.post(
                f"/api/squads/{squad1.id}/members",
                json={
                    "user_id": test_user.discord_id
                },
                headers=headers
            )
            assert response.status_code == 201

            # Get eligible squads again
            response = await client.get(
                f"/api/users/{test_user.discord_id}/eligible-squads?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()
            assert len(data["squads"]) == 1  # Should only be eligible for Silver now
            assert data["squads"][0]["name"] == "Silver Squad"

        finally:
            # Clean up test data
            test_db.query(SquadMember).filter(SquadMember.squad_id.in_([squad1.id, squad2.id, squad3.id])).delete()
            test_db.query(Bytes).filter(Bytes.receiver_id == test_user.id).delete()
            test_db.query(Squad).filter(Squad.guild_id == test_guild.id).delete()
            test_db.query(DiscordUser).filter(DiscordUser.id == test_user.id).delete()
            system_user = test_db.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
            if system_user:
                test_db.query(DiscordUser).filter(DiscordUser.id == system_user.id).delete()
            test_db.query(Guild).filter(Guild.id == test_guild.id).delete()
            test_db.commit()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
