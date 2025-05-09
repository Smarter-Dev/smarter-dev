"""
Integration tests for the bytes role awarding system using httpx.AsyncClient.

This test suite verifies that roles are awarded based on total bytes received
rather than current bytes balance by making direct requests to the API endpoints.
"""

import pytest
import os
import sys
import json
import asyncio
from datetime import datetime
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
import httpx
from starlette.testclient import TestClient

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from website.models import Base, DiscordUser, Bytes, Guild, BytesRole
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
        bytes_create, bytes_list, bytes_detail, bytes_config_get,
        bytes_config_create, bytes_config_update, bytes_roles_list,
        bytes_role_create, bytes_role_update, bytes_role_delete,
        user_bytes_balance
    )

    # Create a test app with the API routes we need
    test_app = Starlette(
        debug=True,
        routes=[
            Route("/api/bytes", bytes_create, methods=["POST"]),
            Route("/api/bytes", bytes_list, methods=["GET"]),
            Route("/api/bytes/{bytes_id:int}", bytes_detail, methods=["GET"]),
            Route("/api/bytes/balance/{user_id}", user_bytes_balance, methods=["GET"]),
            Route("/api/bytes/config/{guild_id}", bytes_config_get, methods=["GET"]),
            Route("/api/bytes/config", bytes_config_create, methods=["POST"]),
            Route("/api/bytes/config/{guild_id}", bytes_config_update, methods=["PUT"]),
            Route("/api/bytes/roles/{guild_id}", bytes_roles_list, methods=["GET"]),
            Route("/api/bytes/roles", bytes_role_create, methods=["POST"]),
            Route("/api/bytes/roles/{role_id:int}", bytes_role_update, methods=["PUT"]),
            Route("/api/bytes/roles/{role_id:int}", bytes_role_delete, methods=["DELETE"]),
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
async def test_roles_based_on_received_not_balance(async_client, test_db, api_token):
    """Test that roles are awarded based on bytes received, not balance."""
    # Use the client as a context manager to ensure it's closed properly
    async with async_client as client:
        # Create test data
        test_user = DiscordUser(
            discord_id=123456789,
            username="test_user",
            bytes_balance=0
        )
        test_db.add(test_user)

        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        test_db.add(test_guild)
        test_db.commit()

        # Create test roles with different byte requirements
        role1 = BytesRole(
            guild_id=test_guild.id,
            role_id=111222333,
            role_name="Bronze",
            bytes_required=100
        )
        role2 = BytesRole(
            guild_id=test_guild.id,
            role_id=444555666,
            role_name="Silver",
            bytes_required=200
        )
        role3 = BytesRole(
            guild_id=test_guild.id,
            role_id=777888999,
            role_name="Gold",
            bytes_required=300
        )
        test_db.add_all([role1, role2, role3])

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

        try:
            # First, give the user 150 bytes (enough for Bronze role)
            headers = {"Authorization": f"Bearer {api_token}"}
            response = await client.post(
                "/api/bytes",
                json={
                    "giver_id": system_user.discord_id,
                    "receiver_id": test_user.discord_id,
                    "guild_id": test_guild.id,
                    "amount": 150,
                    "reason": "Initial bytes"
                },
                headers=headers
            )
            assert response.status_code == 201
            data = response.json()

            # Verify the user has the Bronze role
            assert "earned_roles" in data
            assert len(data["earned_roles"]) == 1
            assert data["earned_roles"][0]["role_name"] == "Bronze"

            # Check the user's bytes balance
            response = await client.get(
                f"/api/bytes/balance/{test_user.discord_id}?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()

            assert data["bytes_balance"] == 150
            assert data["bytes_received"] == 150
            assert len(data["earned_roles"]) == 1
            assert data["earned_roles"][0]["role_name"] == "Bronze"

            # Create another test user to receive bytes
            recipient = DiscordUser(
                discord_id=555666777,
                username="recipient",
                bytes_balance=0
            )
            test_db.add(recipient)
            test_db.commit()

            # Have the user give away 100 bytes
            response = await client.post(
                "/api/bytes",
                json={
                    "giver_id": test_user.discord_id,
                    "receiver_id": recipient.discord_id,
                    "guild_id": test_guild.id,
                    "amount": 100,
                    "reason": "Giving away bytes"
                },
                headers=headers
            )
            assert response.status_code == 201

            # Check the user's bytes balance again
            response = await client.get(
                f"/api/bytes/balance/{test_user.discord_id}?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()

            # Verify the user still has the Bronze role despite lower balance
            assert data["bytes_balance"] == 50  # 150 - 100
            assert data["bytes_received"] == 150  # Unchanged
            assert len(data["earned_roles"]) == 1
            assert data["earned_roles"][0]["role_name"] == "Bronze"

            # Now give the user more bytes to earn the Silver role
            response = await client.post(
                "/api/bytes",
                json={
                    "giver_id": system_user.discord_id,
                    "receiver_id": test_user.discord_id,
                    "guild_id": test_guild.id,
                    "amount": 100,
                    "reason": "More bytes"
                },
                headers=headers
            )
            assert response.status_code == 201

            # Check the user's bytes balance again
            response = await client.get(
                f"/api/bytes/balance/{test_user.discord_id}?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()

            # Verify the user now has both Bronze and Silver roles
            assert data["bytes_balance"] == 150  # 50 + 100
            assert data["bytes_received"] == 250  # 150 + 100
            assert len(data["earned_roles"]) == 2
            role_names = [role["role_name"] for role in data["earned_roles"]]
            assert "Bronze" in role_names
            assert "Silver" in role_names

        finally:
            # Clean up test data
            test_db.query(Bytes).filter(
                (Bytes.giver_id == system_user.id) |
                (Bytes.receiver_id == test_user.id) |
                (Bytes.receiver_id == recipient.id if 'recipient' in locals() else False)
            ).delete()
            test_db.query(BytesRole).filter(BytesRole.guild_id == test_guild.id).delete()
            if 'recipient' in locals():
                test_db.query(DiscordUser).filter(DiscordUser.id == recipient.id).delete()
            test_db.query(DiscordUser).filter(DiscordUser.id == test_user.id).delete()
            test_db.query(Guild).filter(Guild.id == test_guild.id).delete()
            test_db.commit()


@pytest.mark.asyncio
async def test_create_bytes_returns_roles_based_on_received(async_client, test_db, api_token):
    """Test that creating bytes returns roles based on bytes received."""
    # Use the client as a context manager to ensure it's closed properly
    async with async_client as client:
        # Create test data
        test_user = DiscordUser(
            discord_id=123456789,
            username="test_user",
            bytes_balance=0
        )
        test_db.add(test_user)

        test_guild = Guild(
            discord_id=987654321,
            name="test_guild"
        )
        test_db.add(test_guild)
        test_db.commit()

        # Create test roles with different byte requirements
        role1 = BytesRole(
            guild_id=test_guild.id,
            role_id=111222333,
            role_name="Bronze",
            bytes_required=100
        )
        role2 = BytesRole(
            guild_id=test_guild.id,
            role_id=444555666,
            role_name="Silver",
            bytes_required=200
        )
        test_db.add_all([role1, role2])

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

        try:
            # Give the user 250 bytes (enough for both Bronze and Silver roles)
            headers = {"Authorization": f"Bearer {api_token}"}
            response = await client.post(
                "/api/bytes",
                json={
                    "giver_id": system_user.discord_id,
                    "receiver_id": test_user.discord_id,
                    "guild_id": test_guild.id,
                    "amount": 250,
                    "reason": "Testing roles"
                },
                headers=headers
            )
            assert response.status_code == 201
            data = response.json()

            # Verify the response includes the earned roles
            assert "earned_roles" in data
            assert len(data["earned_roles"]) == 2
            role_names = [role["role_name"] for role in data["earned_roles"]]
            assert "Bronze" in role_names
            assert "Silver" in role_names

            # Create another test user to receive bytes
            recipient = DiscordUser(
                discord_id=555666777,
                username="recipient",
                bytes_balance=0
            )
            test_db.add(recipient)
            test_db.commit()

            # Have the user give away some bytes
            response = await client.post(
                "/api/bytes",
                json={
                    "giver_id": test_user.discord_id,
                    "receiver_id": recipient.discord_id,
                    "guild_id": test_guild.id,
                    "amount": 200,
                    "reason": "Giving away bytes"
                },
                headers=headers
            )
            assert response.status_code == 201

            # Check the user's bytes balance
            response = await client.get(
                f"/api/bytes/balance/{test_user.discord_id}?guild_id={test_guild.discord_id}",
                headers=headers
            )
            assert response.status_code == 200
            data = response.json()

            # Verify the user still has the roles despite lower balance
            assert data["bytes_balance"] == 50  # 250 - 200
            assert data["bytes_received"] == 250  # Unchanged
            assert len(data["earned_roles"]) == 2
            role_names = [role["role_name"] for role in data["earned_roles"]]
            assert "Bronze" in role_names
            assert "Silver" in role_names

        finally:
            # Clean up test data
            test_db.query(Bytes).filter(
                (Bytes.giver_id == system_user.id) |
                (Bytes.receiver_id == test_user.id) |
                (Bytes.receiver_id == recipient.id if 'recipient' in locals() else False)
            ).delete()
            test_db.query(BytesRole).filter(BytesRole.guild_id == test_guild.id).delete()
            if 'recipient' in locals():
                test_db.query(DiscordUser).filter(DiscordUser.id == recipient.id).delete()
            test_db.query(DiscordUser).filter(DiscordUser.id == test_user.id).delete()
            test_db.query(Guild).filter(Guild.id == test_guild.id).delete()
            test_db.commit()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
