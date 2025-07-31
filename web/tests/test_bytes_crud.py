"""Tests for bytes CRUD operations."""

import pytest
from datetime import date, timedelta
from sqlalchemy.ext.asyncio import AsyncSession

from web.crud.bytes import bytes_crud, transaction_crud, config_crud


@pytest.mark.asyncio
async def test_get_or_create_balance_new_user(mock_db: AsyncSession):
    """Test creating balance for new user."""
    balance = await bytes_crud.get_or_create_balance(
        mock_db, "guild1", "user1", starting_balance=150
    )

    assert balance.guild_id == "guild1"
    assert balance.user_id == "user1"
    assert balance.balance == 150
    assert balance.total_received == 0
    assert balance.total_sent == 0
    assert balance.streak_count == 0
    assert balance.last_daily is None


@pytest.mark.asyncio
async def test_get_or_create_balance_existing_user(test_db: AsyncSession):
    """Test getting existing balance."""
    # Create initial balance
    balance1 = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )

    # Get same balance again
    balance2 = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=200
    )

    assert balance1.id == balance2.id
    assert balance2.balance == 100  # Should not change


@pytest.mark.asyncio
async def test_update_balance_received(test_db: AsyncSession):
    """Test updating balance for received bytes."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )

    updated = await bytes_crud.update_balance(
        test_db, "guild1", "user1", 50, is_received=True
    )

    assert updated.balance == 150
    assert updated.total_received == 50
    assert updated.total_sent == 0


@pytest.mark.asyncio
async def test_update_balance_sent(test_db: AsyncSession):
    """Test updating balance for sent bytes."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )

    updated = await bytes_crud.update_balance(
        test_db, "guild1", "user1", -30, is_received=False
    )

    assert updated.balance == 70
    assert updated.total_received == 0
    assert updated.total_sent == 30


@pytest.mark.asyncio
async def test_award_daily_new_streak(test_db: AsyncSession):
    """Test awarding daily bytes with new streak."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )

    today = date.today()
    updated, streak = await bytes_crud.award_daily(
        test_db, "guild1", "user1", 25, today
    )

    assert updated.balance == 125
    assert updated.total_received == 25
    assert updated.streak_count == 1
    assert updated.last_daily == today
    assert streak == 1


@pytest.mark.asyncio
async def test_award_daily_continue_streak(test_db: AsyncSession):
    """Test awarding daily bytes with continuing streak."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )

    # Set yesterday as last daily
    yesterday = date.today() - timedelta(days=1)
    balance.last_daily = yesterday
    balance.streak_count = 5
    await test_db.commit()

    today = date.today()
    updated, streak = await bytes_crud.award_daily(
        test_db, "guild1", "user1", 25, today
    )

    assert updated.streak_count == 6
    assert streak == 6


@pytest.mark.asyncio
async def test_award_daily_broken_streak(test_db: AsyncSession):
    """Test awarding daily bytes with broken streak."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )

    # Set 3 days ago as last daily
    three_days_ago = date.today() - timedelta(days=3)
    balance.last_daily = three_days_ago
    balance.streak_count = 10
    await test_db.commit()

    today = date.today()
    updated, streak = await bytes_crud.award_daily(
        test_db, "guild1", "user1", 25, today
    )

    assert updated.streak_count == 1  # Reset to 1
    assert streak == 1


@pytest.mark.asyncio
async def test_get_leaderboard(test_db: AsyncSession):
    """Test getting guild leaderboard."""
    # Create users with different balances
    await bytes_crud.get_or_create_balance(test_db, "guild1", "user1", 500)
    await bytes_crud.get_or_create_balance(test_db, "guild1", "user2", 300)
    await bytes_crud.get_or_create_balance(test_db, "guild1", "user3", 800)
    await bytes_crud.get_or_create_balance(
        test_db, "guild2", "user4", 1000
    )  # Different guild

    leaderboard = await bytes_crud.get_leaderboard(test_db, "guild1", limit=5)

    assert len(leaderboard) == 3
    assert leaderboard[0].user_id == "user3"  # Highest balance
    assert leaderboard[0].balance == 800
    assert leaderboard[1].user_id == "user1"
    assert leaderboard[1].balance == 500
    assert leaderboard[2].user_id == "user2"
    assert leaderboard[2].balance == 300


@pytest.mark.asyncio
async def test_transfer_bytes_success(test_db: AsyncSession):
    """Test successful bytes transfer."""
    # Create giver with balance
    giver = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "giver", starting_balance=1000
    )
    giver.balance = 1000
    giver.total_received = 1000
    await test_db.commit()

    # Transfer bytes
    result = await bytes_crud.transfer_bytes(
        test_db, "giver", "receiver", "guild1", 100, "test transfer"
    )

    assert result["giver_balance"] == 900
    assert result["receiver_balance"] == 200  # 100 starting + 100 transferred

    # Check giver balance
    giver_updated = await bytes_crud.get_user_balance(test_db, "giver", "guild1")
    assert giver_updated.balance == 900
    assert giver_updated.total_sent == 100

    # Check receiver balance
    receiver = await bytes_crud.get_user_balance(test_db, "receiver", "guild1")
    assert receiver.balance == 200
    assert receiver.total_received == 200  # 100 starting + 100 transferred


@pytest.mark.asyncio
async def test_transfer_bytes_insufficient_balance(test_db: AsyncSession):
    """Test transfer with insufficient balance."""
    # Create giver with low balance
    await bytes_crud.get_or_create_balance(
        test_db, "guild1", "giver", starting_balance=50
    )

    # Try to transfer more than available
    with pytest.raises(ValueError, match="Insufficient balance"):
        await bytes_crud.transfer_bytes(test_db, "giver", "receiver", "guild1", 100)


@pytest.mark.asyncio
async def test_get_user_balance(test_db: AsyncSession):
    """Test getting specific user balance."""
    await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=250
    )

    balance = await bytes_crud.get_user_balance(test_db, "user1", "guild1")
    assert balance is not None
    assert balance.balance == 250

    # Test non-existent user
    no_balance = await bytes_crud.get_user_balance(test_db, "nonexistent", "guild1")
    assert no_balance is None


@pytest.mark.asyncio
async def test_get_total_received(test_db: AsyncSession):
    """Test getting total received bytes."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )
    balance.total_received = 500
    await test_db.commit()

    total = await bytes_crud.get_total_received(test_db, "user1", "guild1")
    assert total == 500

    # Test non-existent user
    no_total = await bytes_crud.get_total_received(test_db, "nonexistent", "guild1")
    assert no_total == 0


@pytest.mark.asyncio
async def test_get_total_sent(test_db: AsyncSession):
    """Test getting total sent bytes."""
    balance = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "user1", starting_balance=100
    )
    balance.total_sent = 200
    await test_db.commit()

    total = await bytes_crud.get_total_sent(test_db, "user1", "guild1")
    assert total == 200

    # Test non-existent user
    no_total = await bytes_crud.get_total_sent(test_db, "nonexistent", "guild1")
    assert no_total == 0


@pytest.mark.asyncio
async def test_guild_config_operations(test_db: AsyncSession):
    """Test guild configuration CRUD operations."""
    # Test getting non-existent config
    config = await bytes_crud.get_guild_config(test_db, "guild1")
    assert config is None

    # Create config using config_crud
    config_data = await config_crud.create(
        test_db,
        guild_id="guild1",
        starting_balance=200,
        daily_amount=25,
        max_transfer=2000,
        cooldown_hours=18,
        role_rewards={"role1": 1000, "role2": 5000},
    )

    # Test getting existing config
    config = await bytes_crud.get_guild_config(test_db, "guild1")
    assert config is not None
    assert config.starting_balance == 200
    assert config.daily_amount == 25
    assert config.max_transfer == 2000
    assert config.cooldown_hours == 18
    assert config.role_rewards == {"role1": 1000, "role2": 5000}


@pytest.mark.asyncio
async def test_transaction_logging(test_db: AsyncSession):
    """Test that transfers create transaction records."""
    # Create giver with balance
    giver = await bytes_crud.get_or_create_balance(
        test_db, "guild1", "giver", starting_balance=1000
    )
    giver.balance = 1000
    await test_db.commit()

    # Transfer bytes
    await bytes_crud.transfer_bytes(
        test_db, "giver", "receiver", "guild1", 150, "test transaction"
    )

    # Check transaction was created
    transactions = await transaction_crud.get_multi(test_db, guild_id="guild1")
    assert len(transactions) == 1

    transaction = transactions[0]
    assert transaction.giver_id == "giver"
    assert transaction.receiver_id == "receiver"
    assert transaction.guild_id == "guild1"
    assert transaction.amount == 150
    assert transaction.reason == "test transaction"
