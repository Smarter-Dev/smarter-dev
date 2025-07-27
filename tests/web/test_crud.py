"""Comprehensive tests for CRUD operations.

Tests all database operations following TDD methodology with full coverage
of happy paths, error cases, and edge conditions. Tests are organized by
operation class and follow the Arrange-Act-Assert pattern.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta, date
from uuid import uuid4
from unittest.mock import Mock

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.crud import (
    BytesOperations,
    BytesConfigOperations,
    SquadOperations,
    DatabaseOperationError,
    NotFoundError,
    ConflictError,
)
from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction,
    BytesConfig,
    Squad,
    SquadMembership,
)


class TestBytesOperations:
    """Test cases for BytesOperations CRUD class.
    
    Tests follow TDD methodology with comprehensive coverage of all business
    logic and error conditions. Each test focuses on a single operation.
    """
    
    @pytest.fixture
    def bytes_ops(self):
        """Create BytesOperations instance for testing."""
        return BytesOperations()
    
    @pytest.fixture
    async def sample_config(self, db_session: AsyncSession):
        """Create sample bytes config for testing."""
        config = BytesConfig(
            guild_id="test_guild_123",
            starting_balance=100,
            daily_amount=10,
            streak_bonuses={"7": 2, "14": 4},
            max_transfer=1000,
            transfer_cooldown_hours=0,
            role_rewards={}
        )
        db_session.add(config)
        await db_session.commit()
        await db_session.refresh(config)
        return config
    
    async def test_get_balance_existing_user(self, bytes_ops, db_session: AsyncSession):
        """Test getting balance for existing user."""
        # Arrange
        expected_balance = BytesBalance(
            guild_id="test_guild_123",
            user_id="test_user_456",
            balance=150,
            total_received=200,
            total_sent=50,
            streak_count=5
        )
        db_session.add(expected_balance)
        await db_session.commit()
        
        # Act
        result = await bytes_ops.get_balance(db_session, "test_guild_123", "test_user_456")
        
        # Assert
        assert result.guild_id == "test_guild_123"
        assert result.user_id == "test_user_456"
        assert result.balance == 150
        assert result.total_received == 200
        assert result.total_sent == 50
        assert result.streak_count == 5
    
    async def test_get_balance_new_user_with_config(
        self, 
        bytes_ops, 
        db_session: AsyncSession, 
        sample_config
    ):
        """Test getting balance for new user with existing guild config."""
        # Act
        result = await bytes_ops.get_balance(db_session, "test_guild_123", "new_user_789")
        
        # Assert
        assert result.guild_id == "test_guild_123"
        assert result.user_id == "new_user_789"
        assert result.balance == 100  # From config starting_balance
        assert result.total_received == 100  # Starting balance counts as received
        assert result.total_sent == 0
        assert result.streak_count == 0
        assert result.last_daily is None
    
    async def test_get_balance_new_user_new_guild(self, bytes_ops, db_session: AsyncSession):
        """Test getting balance for new user in new guild (creates default config)."""
        # Act
        result = await bytes_ops.get_balance(db_session, "new_guild_999", "new_user_888")
        
        # Assert
        assert result.guild_id == "new_guild_999"
        assert result.user_id == "new_user_888"
        assert result.balance == 100  # Default starting balance
        assert result.total_received == 100  # Starting balance counts as received
        assert result.total_sent == 0
        assert result.streak_count == 0
    
    async def test_create_transaction_successful(self, bytes_ops, db_session: AsyncSession):
        """Test successful transaction creation with balance updates."""
        # Arrange
        giver = BytesBalance(
            guild_id="test_guild_123",
            user_id="giver_user_123",
            balance=200,
            total_received=200,
            total_sent=0
        )
        receiver = BytesBalance(
            guild_id="test_guild_123",
            user_id="receiver_user_456",
            balance=50,
            total_received=50,
            total_sent=0
        )
        db_session.add_all([giver, receiver])
        await db_session.commit()
        
        # Act
        transaction = await bytes_ops.create_transaction(
            db_session,
            guild_id="test_guild_123",
            giver_id="giver_user_123",
            giver_username="GiverUser",
            receiver_id="receiver_user_456",
            receiver_username="ReceiverUser",
            amount=75,
            reason="Test transaction"
        )
        
        # Commit to see the changes
        await db_session.commit()
        
        # Assert transaction
        assert transaction.guild_id == "test_guild_123"
        assert transaction.giver_id == "giver_user_123"
        assert transaction.giver_username == "GiverUser"
        assert transaction.receiver_id == "receiver_user_456"
        assert transaction.receiver_username == "ReceiverUser"
        assert transaction.amount == 75
        assert transaction.reason == "Test transaction"
        assert transaction.id is not None
        
        # Assert balance updates
        await db_session.refresh(giver)
        await db_session.refresh(receiver)
        
        assert giver.balance == 125  # 200 - 75
        assert giver.total_sent == 75
        assert giver.total_received == 200  # Unchanged
        
        assert receiver.balance == 125  # 50 + 75
        assert receiver.total_received == 125  # 50 + 75
        assert receiver.total_sent == 0  # Unchanged
    
    async def test_create_transaction_insufficient_balance(
        self, 
        bytes_ops, 
        db_session: AsyncSession
    ):
        """Test transaction creation with insufficient balance."""
        # Arrange
        giver = BytesBalance(
            guild_id="test_guild_123",
            user_id="poor_user_123",
            balance=30,
            total_received=30,
            total_sent=0
        )
        receiver = BytesBalance(
            guild_id="test_guild_123",
            user_id="receiver_user_456",
            balance=50,
            total_received=50,
            total_sent=0
        )
        db_session.add_all([giver, receiver])
        await db_session.commit()
        
        # Act & Assert
        with pytest.raises(ConflictError, match="Insufficient balance: 30 < 50"):
            await bytes_ops.create_transaction(
                db_session,
                guild_id="test_guild_123",
                giver_id="poor_user_123",
                giver_username="PoorUser",
                receiver_id="receiver_user_456",
                receiver_username="ReceiverUser",
                amount=50
            )
        
        # Assert balances unchanged
        await db_session.refresh(giver)
        await db_session.refresh(receiver)
        assert giver.balance == 30
        assert receiver.balance == 50
    
    async def test_create_transaction_new_users(
        self, 
        bytes_ops, 
        db_session: AsyncSession, 
        sample_config
    ):
        """Test transaction between new users (auto-creates balances)."""
        # Act
        transaction = await bytes_ops.create_transaction(
            db_session,
            guild_id="test_guild_123",
            giver_id="new_giver_123",
            giver_username="NewGiver",
            receiver_id="new_receiver_456",
            receiver_username="NewReceiver",
            amount=25
        )
        
        # Commit to see the changes
        await db_session.commit()
        
        # Assert transaction created
        assert transaction.amount == 25
        assert transaction.giver_id == "new_giver_123"
        assert transaction.receiver_id == "new_receiver_456"
        
        # Assert balances were created and updated
        giver_balance = await bytes_ops.get_balance(db_session, "test_guild_123", "new_giver_123")
        receiver_balance = await bytes_ops.get_balance(db_session, "test_guild_123", "new_receiver_456")
        
        assert giver_balance.balance == 75  # 100 starting - 25 sent
        assert giver_balance.total_sent == 25
        assert receiver_balance.balance == 125  # 100 starting + 25 received
        assert receiver_balance.total_received == 125
    
    async def test_get_leaderboard_ordered_correctly(self, bytes_ops, db_session: AsyncSession):
        """Test leaderboard returns users ordered by balance descending."""
        # Arrange
        balances = [
            BytesBalance(guild_id="test_guild_123", user_id="user_1", balance=500),
            BytesBalance(guild_id="test_guild_123", user_id="user_2", balance=250),
            BytesBalance(guild_id="test_guild_123", user_id="user_3", balance=750),
            BytesBalance(guild_id="test_guild_123", user_id="user_4", balance=100),
            BytesBalance(guild_id="other_guild_456", user_id="user_5", balance=1000),  # Different guild
        ]
        db_session.add_all(balances)
        await db_session.commit()
        
        # Act
        leaderboard = await bytes_ops.get_leaderboard(db_session, "test_guild_123", limit=3)
        
        # Assert
        assert len(leaderboard) == 3
        assert leaderboard[0].user_id == "user_3"  # 750
        assert leaderboard[0].balance == 750
        assert leaderboard[1].user_id == "user_1"  # 500
        assert leaderboard[1].balance == 500
        assert leaderboard[2].user_id == "user_2"  # 250
        assert leaderboard[2].balance == 250
        
        # Verify other guild user not included
        user_ids = [b.user_id for b in leaderboard]
        assert "user_5" not in user_ids
    
    async def test_get_leaderboard_empty_guild(self, bytes_ops, db_session: AsyncSession):
        """Test leaderboard for guild with no balances."""
        # Act
        leaderboard = await bytes_ops.get_leaderboard(db_session, "empty_guild_999")
        
        # Assert
        assert leaderboard == []
    
    async def test_get_transaction_history_guild_filter(self, bytes_ops, db_session: AsyncSession):
        """Test transaction history filtered by guild."""
        # Arrange
        transactions = [
            BytesTransaction(
                guild_id="test_guild_123",
                giver_id="user_1",
                giver_username="User1",
                receiver_id="user_2",
                receiver_username="User2",
                amount=50
            ),
            BytesTransaction(
                guild_id="test_guild_123",
                giver_id="user_2",
                giver_username="User2",
                receiver_id="user_3",
                receiver_username="User3",
                amount=25
            ),
            BytesTransaction(
                guild_id="other_guild_456",
                giver_id="user_4",
                giver_username="User4",
                receiver_id="user_5",
                receiver_username="User5",
                amount=100
            ),
        ]
        db_session.add_all(transactions)
        await db_session.commit()
        
        # Act
        history = await bytes_ops.get_transaction_history(db_session, "test_guild_123")
        
        # Assert
        assert len(history) == 2
        guild_ids = [t.guild_id for t in history]
        assert all(guild_id == "test_guild_123" for guild_id in guild_ids)
    
    async def test_get_transaction_history_user_filter(self, bytes_ops, db_session: AsyncSession):
        """Test transaction history filtered by user."""
        # Arrange
        transactions = [
            BytesTransaction(
                guild_id="test_guild_123",
                giver_id="target_user_123",
                giver_username="TargetUser",
                receiver_id="user_2",
                receiver_username="User2",
                amount=50
            ),
            BytesTransaction(
                guild_id="test_guild_123",
                giver_id="user_3",
                giver_username="User3",
                receiver_id="target_user_123",
                receiver_username="TargetUser",
                amount=25
            ),
            BytesTransaction(
                guild_id="test_guild_123",
                giver_id="user_4",
                giver_username="User4",
                receiver_id="user_5",
                receiver_username="User5",
                amount=100
            ),
        ]
        db_session.add_all(transactions)
        await db_session.commit()
        
        # Act
        history = await bytes_ops.get_transaction_history(
            db_session, 
            "test_guild_123", 
            user_id="target_user_123"
        )
        
        # Assert
        assert len(history) == 2
        for transaction in history:
            assert ("target_user_123" == transaction.giver_id or 
                   "target_user_123" == transaction.receiver_id)
    
    async def test_update_daily_reward_new_streak(self, bytes_ops, db_session: AsyncSession):
        """Test daily reward update for user with no previous streak."""
        # Arrange
        balance = BytesBalance(
            guild_id="test_guild_123",
            user_id="daily_user_123",
            balance=100,
            total_received=100,
            streak_count=0,
            last_daily=None
        )
        db_session.add(balance)
        await db_session.commit()
        
        # Act
        updated_balance = await bytes_ops.update_daily_reward(
            db_session,
            guild_id="test_guild_123",
            user_id="daily_user_123",
            daily_amount=10,
            streak_bonus=2
        )
        
        # Assert
        assert updated_balance.balance == 120  # 100 + (10 * 2)
        assert updated_balance.total_received == 120  # 100 + 20
        assert updated_balance.streak_count == 1  # Incremented
        assert updated_balance.last_daily is not None
        assert updated_balance.last_daily == date.today()
    
    async def test_update_daily_reward_existing_streak(self, bytes_ops, db_session: AsyncSession):
        """Test daily reward update for user with existing streak."""
        # Arrange
        yesterday = date.today() - timedelta(days=1)
        balance = BytesBalance(
            guild_id="test_guild_123",
            user_id="streak_user_123",
            balance=150,
            total_received=200,
            streak_count=7,
            last_daily=yesterday
        )
        db_session.add(balance)
        await db_session.commit()
        
        # Act
        updated_balance = await bytes_ops.update_daily_reward(
            db_session,
            guild_id="test_guild_123",
            user_id="streak_user_123",
            daily_amount=10,
            streak_bonus=4  # 7-day streak bonus
        )
        
        # Assert
        assert updated_balance.balance == 190  # 150 + (10 * 4)
        assert updated_balance.total_received == 240  # 200 + 40
        assert updated_balance.streak_count == 8  # Incremented from 7
        assert updated_balance.last_daily == date.today()
    
    async def test_reset_streak(self, bytes_ops, db_session: AsyncSession):
        """Test resetting user's daily streak."""
        # Arrange
        balance = BytesBalance(
            guild_id="test_guild_123",
            user_id="reset_user_123",
            balance=150,
            streak_count=14,
            last_daily=date.today() - timedelta(days=2)
        )
        db_session.add(balance)
        await db_session.commit()
        
        # Act
        updated_balance = await bytes_ops.reset_streak(
            db_session,
            "test_guild_123",
            "reset_user_123"
        )
        
        # Assert
        assert updated_balance.streak_count == 0
        assert updated_balance.balance == 150  # Unchanged
        assert updated_balance.last_daily is not None  # Unchanged


class TestBytesConfigOperations:
    """Test cases for BytesConfigOperations CRUD class."""
    
    @pytest.fixture
    def config_ops(self):
        """Create BytesConfigOperations instance for testing."""
        return BytesConfigOperations()
    
    async def test_get_config_existing(self, config_ops, db_session: AsyncSession):
        """Test getting existing configuration."""
        # Arrange
        config = BytesConfig(
            guild_id="test_guild_123",
            starting_balance=200,
            daily_amount=15,
            streak_bonuses={"10": 3, "20": 5},
            max_transfer=2000,
            transfer_cooldown_hours=1,
            role_rewards={"role_123": 50}
        )
        db_session.add(config)
        await db_session.commit()
        
        # Act
        result = await config_ops.get_config(db_session, "test_guild_123")
        
        # Assert
        assert result.guild_id == "test_guild_123"
        assert result.starting_balance == 200
        assert result.daily_amount == 15
        assert result.streak_bonuses == {"10": 3, "20": 5}
        assert result.max_transfer == 2000
        assert result.transfer_cooldown_hours == 1
        assert result.role_rewards == {"role_123": 50}
    
    async def test_get_config_not_found(self, config_ops, db_session: AsyncSession):
        """Test getting non-existent configuration raises NotFoundError."""
        # Act & Assert
        with pytest.raises(NotFoundError, match="Configuration not found for guild nonexistent_guild"):
            await config_ops.get_config(db_session, "nonexistent_guild")
    
    async def test_create_config_successful(self, config_ops, db_session: AsyncSession):
        """Test successful configuration creation."""
        # Act
        config = await config_ops.create_config(
            db_session,
            guild_id="new_guild_123",
            starting_balance=150,
            daily_amount=12,
            max_transfer=800
        )
        
        # Assert
        assert config.guild_id == "new_guild_123"
        assert config.starting_balance == 150
        assert config.daily_amount == 12
        assert config.max_transfer == 800
        
        # Verify it was saved to database
        saved_config = await config_ops.get_config(db_session, "new_guild_123")
        assert saved_config.starting_balance == 150
    
    async def test_create_config_duplicate_guild(self, config_ops, db_session: AsyncSession, unique_guild_id):
        """Test creating configuration for existing guild raises ConflictError."""
        # Arrange
        existing_config = BytesConfig(guild_id=unique_guild_id)
        db_session.add(existing_config)
        await db_session.commit()
        
        # Clear identity map to avoid conflicts
        db_session.expunge_all()
        
        # Act & Assert
        with pytest.raises(ConflictError, match=f"Configuration already exists for guild {unique_guild_id}"):
            await config_ops.create_config(db_session, guild_id=unique_guild_id)
    
    async def test_update_config_successful(self, config_ops, db_session: AsyncSession):
        """Test successful configuration update."""
        # Arrange
        config = BytesConfig(
            guild_id="update_guild_123",
            starting_balance=100,
            daily_amount=10,
            max_transfer=1000
        )
        db_session.add(config)
        await db_session.commit()
        
        # Act
        updated_config = await config_ops.update_config(
            db_session,
            guild_id="update_guild_123",
            starting_balance=250,
            daily_amount=20
        )
        
        # Assert
        assert updated_config.starting_balance == 250
        assert updated_config.daily_amount == 20
        assert updated_config.max_transfer == 1000  # Unchanged
        
        # Verify persistence
        saved_config = await config_ops.get_config(db_session, "update_guild_123")
        assert saved_config.starting_balance == 250
        assert saved_config.daily_amount == 20
    
    async def test_update_config_not_found(self, config_ops, db_session: AsyncSession):
        """Test updating non-existent configuration raises NotFoundError."""
        # Act & Assert
        with pytest.raises(NotFoundError, match="Configuration not found for guild nonexistent_guild"):
            await config_ops.update_config(
                db_session,
                guild_id="nonexistent_guild",
                starting_balance=200
            )
    
    async def test_delete_config_successful(self, config_ops, db_session: AsyncSession):
        """Test successful configuration deletion."""
        # Arrange
        config = BytesConfig(guild_id="delete_guild_123")
        db_session.add(config)
        await db_session.commit()
        
        # Act
        await config_ops.delete_config(db_session, "delete_guild_123")
        
        # Assert
        with pytest.raises(NotFoundError):
            await config_ops.get_config(db_session, "delete_guild_123")
    
    async def test_delete_config_not_found(self, config_ops, db_session: AsyncSession):
        """Test deleting non-existent configuration raises NotFoundError."""
        # Act & Assert
        with pytest.raises(NotFoundError, match="Configuration not found for guild nonexistent_guild"):
            await config_ops.delete_config(db_session, "nonexistent_guild")


class TestSquadOperations:
    """Test cases for SquadOperations CRUD class."""
    
    @pytest.fixture
    def squad_ops(self):
        """Create SquadOperations instance for testing."""
        return SquadOperations()
    
    @pytest.fixture
    async def sample_squad(self, db_session: AsyncSession):
        """Create sample squad for testing."""
        squad = Squad(
            guild_id="test_guild_123",
            role_id="test_role_456",
            name="Test Squad",
            description="A test squad",
            switch_cost=50,
            max_members=10,
            is_active=True
        )
        db_session.add(squad)
        await db_session.commit()
        await db_session.refresh(squad)
        return squad
    
    async def test_get_squad_existing(self, squad_ops, db_session: AsyncSession, sample_squad):
        """Test getting existing squad by ID."""
        # Act
        result = await squad_ops.get_squad(db_session, sample_squad.id)
        
        # Assert
        assert result.id == sample_squad.id
        assert result.guild_id == "test_guild_123"
        assert result.role_id == "test_role_456"
        assert result.name == "Test Squad"
        assert result.description == "A test squad"
        assert result.switch_cost == 50
        assert result.max_members == 10
        assert result.is_active is True
    
    async def test_get_squad_not_found(self, squad_ops, db_session: AsyncSession):
        """Test getting non-existent squad raises NotFoundError."""
        fake_id = uuid4()
        
        # Act & Assert
        with pytest.raises(NotFoundError, match=f"Squad not found: {fake_id}"):
            await squad_ops.get_squad(db_session, fake_id)
    
    async def test_get_guild_squads_active_only(self, squad_ops, db_session: AsyncSession):
        """Test getting active squads for a guild."""
        # Arrange
        squads = [
            Squad(guild_id="test_guild_123", role_id="role_1", name="Active Squad 1", is_active=True),
            Squad(guild_id="test_guild_123", role_id="role_2", name="Active Squad 2", is_active=True),
            Squad(guild_id="test_guild_123", role_id="role_3", name="Inactive Squad", is_active=False),
            Squad(guild_id="other_guild_456", role_id="role_4", name="Other Guild Squad", is_active=True),
        ]
        db_session.add_all(squads)
        await db_session.commit()
        
        # Act
        result = await squad_ops.get_guild_squads(db_session, "test_guild_123", active_only=True)
        
        # Assert
        assert len(result) == 2
        names = [s.name for s in result]
        assert "Active Squad 1" in names
        assert "Active Squad 2" in names
        assert "Inactive Squad" not in names
        assert "Other Guild Squad" not in names
    
    async def test_get_guild_squads_include_inactive(self, squad_ops, db_session: AsyncSession):
        """Test getting all squads for a guild including inactive ones."""
        # Arrange
        squads = [
            Squad(guild_id="test_guild_123", role_id="role_1", name="Active Squad", is_active=True),
            Squad(guild_id="test_guild_123", role_id="role_2", name="Inactive Squad", is_active=False),
        ]
        db_session.add_all(squads)
        await db_session.commit()
        
        # Act
        result = await squad_ops.get_guild_squads(db_session, "test_guild_123", active_only=False)
        
        # Assert
        assert len(result) == 2
        names = [s.name for s in result]
        assert "Active Squad" in names
        assert "Inactive Squad" in names
    
    async def test_create_squad_successful(self, squad_ops, db_session: AsyncSession):
        """Test successful squad creation."""
        # Act
        squad = await squad_ops.create_squad(
            db_session,
            guild_id="new_guild_123",
            role_id="new_role_456",
            name="New Squad",
            description="A new squad",
            switch_cost=25,
            max_members=5
        )
        
        # Assert
        assert squad.guild_id == "new_guild_123"
        assert squad.role_id == "new_role_456"
        assert squad.name == "New Squad"
        assert squad.description == "A new squad"
        assert squad.switch_cost == 25
        assert squad.max_members == 5
        assert squad.is_active is True  # Default value
        assert squad.id is not None
    
    async def test_join_squad_successful(self, squad_ops, db_session: AsyncSession, sample_squad):
        """Test successful squad joining with cost deduction."""
        # Arrange
        user_balance = BytesBalance(
            guild_id="test_guild_123",
            user_id="joining_user_123",
            balance=100
        )
        db_session.add(user_balance)
        await db_session.commit()
        
        # Act
        membership = await squad_ops.join_squad(
            db_session,
            guild_id="test_guild_123",
            user_id="joining_user_123",
            squad_id=sample_squad.id
        )
        
        # Commit to see the changes
        await db_session.commit()
        
        # Assert membership
        assert membership.squad_id == sample_squad.id
        assert membership.user_id == "joining_user_123"
        assert membership.guild_id == "test_guild_123"
        assert membership.joined_at is not None
        
        # Assert cost deduction
        await db_session.refresh(user_balance)
        assert user_balance.balance == 50  # 100 - 50 switch cost
    
    async def test_join_squad_insufficient_balance(
        self, 
        squad_ops, 
        db_session: AsyncSession, 
        sample_squad
    ):
        """Test joining squad with insufficient balance raises ConflictError."""
        # Arrange
        user_balance = BytesBalance(
            guild_id="test_guild_123",
            user_id="poor_user_123",
            balance=25  # Less than 50 switch cost
        )
        db_session.add(user_balance)
        await db_session.commit()
        
        # Act & Assert
        with pytest.raises(ConflictError, match="Insufficient balance: 25 < 50"):
            await squad_ops.join_squad(
                db_session,
                guild_id="test_guild_123",
                user_id="poor_user_123",
                squad_id=sample_squad.id
            )
    
    async def test_join_squad_already_in_squad(
        self, 
        squad_ops, 
        db_session: AsyncSession, 
        sample_squad
    ):
        """Test joining squad when already in another squad raises ConflictError."""
        # Arrange
        other_squad = Squad(
            guild_id="test_guild_123",
            role_id="other_role_789",
            name="Other Squad"
        )
        db_session.add(other_squad)
        await db_session.commit()
        await db_session.refresh(other_squad)
        
        existing_membership = SquadMembership(
            squad_id=other_squad.id,
            user_id="member_user_123",
            guild_id="test_guild_123"
        )
        db_session.add(existing_membership)
        await db_session.commit()
        
        # Act & Assert
        with pytest.raises(ConflictError, match="User already in squad Other Squad"):
            await squad_ops.join_squad(
                db_session,
                guild_id="test_guild_123",
                user_id="member_user_123",
                squad_id=sample_squad.id
            )
    
    async def test_join_squad_squad_full(self, squad_ops, db_session: AsyncSession):
        """Test joining full squad raises ConflictError."""
        # Arrange
        squad = Squad(
            guild_id="test_guild_123",
            role_id="full_role_123",
            name="Full Squad",
            max_members=1  # Very small limit
        )
        db_session.add(squad)
        await db_session.commit()
        await db_session.refresh(squad)
        
        # Fill the squad
        existing_member = SquadMembership(
            squad_id=squad.id,
            user_id="existing_user_123",
            guild_id="test_guild_123"
        )
        db_session.add(existing_member)
        await db_session.commit()
        
        # Act & Assert
        with pytest.raises(ConflictError, match="Squad Full Squad is full"):
            await squad_ops.join_squad(
                db_session,
                guild_id="test_guild_123",
                user_id="new_user_456",
                squad_id=squad.id
            )
    
    async def test_join_squad_inactive_squad(self, squad_ops, db_session: AsyncSession):
        """Test joining inactive squad raises ConflictError."""
        # Arrange
        inactive_squad = Squad(
            guild_id="test_guild_123",
            role_id="inactive_role_123",
            name="Inactive Squad",
            is_active=False
        )
        db_session.add(inactive_squad)
        await db_session.commit()
        await db_session.refresh(inactive_squad)
        
        # Act & Assert
        with pytest.raises(ConflictError, match="Squad Inactive Squad is not active"):
            await squad_ops.join_squad(
                db_session,
                guild_id="test_guild_123",
                user_id="user_123",
                squad_id=inactive_squad.id
            )
    
    async def test_leave_squad_successful(self, squad_ops, db_session: AsyncSession, sample_squad):
        """Test successful squad leaving."""
        # Arrange
        membership = SquadMembership(
            squad_id=sample_squad.id,
            user_id="leaving_user_123",
            guild_id="test_guild_123"
        )
        db_session.add(membership)
        await db_session.commit()
        
        # Act
        await squad_ops.leave_squad(db_session, "test_guild_123", "leaving_user_123")
        
        # Assert
        user_squad = await squad_ops.get_user_squad(db_session, "test_guild_123", "leaving_user_123")
        assert user_squad is None
    
    async def test_leave_squad_not_in_squad(self, squad_ops, db_session: AsyncSession):
        """Test leaving squad when not in any squad raises NotFoundError."""
        # Act & Assert
        with pytest.raises(NotFoundError, match="User not_member_123 not in any squad"):
            await squad_ops.leave_squad(db_session, "test_guild_123", "not_member_123")
    
    async def test_get_user_squad_existing(self, squad_ops, db_session: AsyncSession, sample_squad):
        """Test getting user's current squad."""
        # Arrange
        membership = SquadMembership(
            squad_id=sample_squad.id,
            user_id="member_user_123",
            guild_id="test_guild_123"
        )
        db_session.add(membership)
        await db_session.commit()
        
        # Act
        result = await squad_ops.get_user_squad(db_session, "test_guild_123", "member_user_123")
        
        # Assert
        assert result is not None
        assert result.id == sample_squad.id
        assert result.name == "Test Squad"
    
    async def test_get_user_squad_not_in_squad(self, squad_ops, db_session: AsyncSession):
        """Test getting user squad when not in any squad returns None."""
        # Act
        result = await squad_ops.get_user_squad(db_session, "test_guild_123", "no_squad_user_123")
        
        # Assert
        assert result is None
    
    async def test_get_squad_members(self, squad_ops, db_session: AsyncSession, sample_squad):
        """Test getting all members of a squad."""
        # Arrange
        memberships = [
            SquadMembership(
                squad_id=sample_squad.id,
                user_id="member_1",
                guild_id="test_guild_123"
            ),
            SquadMembership(
                squad_id=sample_squad.id,
                user_id="member_2",
                guild_id="test_guild_123"
            ),
        ]
        db_session.add_all(memberships)
        await db_session.commit()
        
        # Act
        members = await squad_ops.get_squad_members(db_session, sample_squad.id)
        
        # Assert
        assert len(members) == 2
        user_ids = [m.user_id for m in members]
        assert "member_1" in user_ids
        assert "member_2" in user_ids
        assert all(m.squad_id == sample_squad.id for m in members)