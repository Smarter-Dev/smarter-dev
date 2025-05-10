"""
Test that squad switching correctly deducts bytes from balance but not from received bytes.
"""

import os
import pytest
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from website.models import Guild, DiscordUser, Squad, SquadMember, BytesConfig, Bytes
from website.database import get_db, engine
from website.app import app


@pytest.mark.asyncio
async def test_squad_switch_bytes_deduction():
    """Test that squad switching deducts bytes from balance but not from received bytes."""
    # Set environment variable for local development
    os.environ["SMARTER_DEV_LOCAL"] = "1"

    # Create a test client
    client = TestClient(app)

    # Create a session
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Get or create test guild
        test_guild = session.query(Guild).filter(Guild.discord_id == 987654321).first()
        if not test_guild:
            test_guild = Guild(
                discord_id=987654321,
                name="test_guild"
            )
            session.add(test_guild)
            session.commit()

        # Get or create bytes config
        bytes_config = session.query(BytesConfig).filter(BytesConfig.guild_id == test_guild.id).first()
        if not bytes_config:
            bytes_config = BytesConfig(
                guild_id=test_guild.id,
                squad_join_bytes_required=0,  # No bytes required for testing
                squad_switch_cost=50  # Cost to switch squads
            )
            session.add(bytes_config)
            session.commit()

        # Get or create system user
        system_user = session.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
        if not system_user:
            system_user = DiscordUser(
                discord_id=0,
                username="System",
                bytes_balance=999999
            )
            session.add(system_user)
            session.commit()

        # Get or create test user
        test_user = session.query(DiscordUser).filter(DiscordUser.discord_id == 123456789).first()
        if not test_user:
            test_user = DiscordUser(
                discord_id=123456789,
                username="test_user",
                bytes_balance=0
            )
            session.add(test_user)
            session.commit()

        # Check if user already has bytes from system
        existing_bytes = session.query(Bytes).filter(
            Bytes.giver_id == system_user.id,
            Bytes.receiver_id == test_user.id,
            Bytes.guild_id == test_guild.id,
            Bytes.reason == "Initial bytes"
        ).first()

        if not existing_bytes:
            # Give the user some bytes
            bytes_transaction = Bytes(
                giver_id=system_user.id,
                receiver_id=test_user.id,
                guild_id=test_guild.id,
                amount=100,
                reason="Initial bytes"
            )
            session.add(bytes_transaction)
            session.commit()

        # Update user's balance
        test_user.bytes_balance = 100
        session.commit()

        # Get or create two squads
        squad1 = session.query(Squad).filter(Squad.guild_id == test_guild.id, Squad.role_id == 111111111).first()
        if not squad1:
            squad1 = Squad(
                guild_id=test_guild.id,
                role_id=111111111,
                name="Squad 1",
                description="First test squad"
            )
            session.add(squad1)
            session.commit()

        squad2 = session.query(Squad).filter(Squad.guild_id == test_guild.id, Squad.role_id == 222222222).first()
        if not squad2:
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
        response_data = response.json()
        api_token = response_data["token"]
        headers = {"Authorization": f"Bearer {api_token}"}

        # Add user to first squad
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

        # Get user's bytes balance and received bytes before switching squads
        initial_balance = test_user.bytes_balance
        assert initial_balance == 100

        # Get initial received bytes
        bytes_received_query = session.query(Bytes).filter(
            Bytes.receiver_id == test_user.id
        ).all()
        initial_received_bytes = sum(b.amount for b in bytes_received_query)
        assert initial_received_bytes == 100

        # Add user to second squad (this should charge the squad switch cost)
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

        # Refresh user to get updated bytes balance
        session.refresh(test_user)

        # Verify bytes were deducted from balance
        assert test_user.bytes_balance == initial_balance - bytes_config.squad_switch_cost
        assert test_user.bytes_balance == 50  # 100 - 50 = 50

        # Get bytes received after switching
        bytes_received_query = session.query(Bytes).filter(
            Bytes.receiver_id == test_user.id
        ).all()
        final_received_bytes = sum(b.amount for b in bytes_received_query)

        # Verify received bytes didn't change
        assert final_received_bytes == initial_received_bytes
        assert final_received_bytes == 100

        # Check the bytes API endpoint to verify the correct values are returned
        response = client.get(
            f"/api/bytes/balance/{test_user.discord_id}",
            headers=headers
        )
        assert response.status_code == 200
        data = response.json()

        # Verify the API returns the correct values
        assert data["bytes_balance"] == 50
        assert data["bytes_received"] == 100
        assert data["bytes_given"] == 50  # The bytes given to the system user

    finally:
        # Clean up
        try:
            # Rollback any pending transactions
            session.rollback()

            # Delete all test data
            session.query(Bytes).delete()
            session.query(SquadMember).delete()
            session.query(Squad).delete()
            session.query(BytesConfig).delete()
            # Don't delete system user (discord_id=0)
            session.query(DiscordUser).filter(DiscordUser.discord_id != 0).delete()
            session.query(Guild).delete()
            session.commit()
        except Exception as e:
            print(f"Error during cleanup: {e}")
        finally:
            session.close()
