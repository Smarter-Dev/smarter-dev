"""Tests for Discord bot embed utilities.

This module tests the embed creation functions to ensure they
generate properly formatted Discord embeds with correct styling.
"""

from __future__ import annotations

import pytest
from datetime import datetime, date
from uuid import uuid4

import hikari

from smarter_dev.bot.utils.embeds import (
    create_balance_embed,
    create_error_embed,
    create_success_embed,
    create_warning_embed,
    create_info_embed,
    create_leaderboard_embed,
    create_transaction_history_embed,
    create_squad_list_embed,
    get_streak_name
)
from smarter_dev.bot.services.models import BytesBalance, BytesTransaction


@pytest.fixture
def sample_balance():
    """Create a sample bytes balance for testing."""
    return BytesBalance(
        guild_id="123456789",
        user_id="987654321",
        balance=1500,
        total_received=3000,
        total_sent=1500,
        streak_count=15,
        last_daily=date(2024, 1, 15),
        created_at=datetime.now(),
        updated_at=datetime.now()
    )


@pytest.fixture
def sample_transactions():
    """Create sample transactions for testing."""
    return [
        BytesTransaction(
            id=uuid4(),
            guild_id="123456789",
            giver_id="987654321",
            giver_username="TestUser",
            receiver_id="111222333",
            receiver_username="OtherUser",
            amount=100,
            reason="Test transfer",
            created_at=datetime(2024, 1, 15, 10, 30)
        ),
        BytesTransaction(
            id=uuid4(),
            guild_id="123456789",
            giver_id="111222333",
            giver_username="OtherUser",
            receiver_id="987654321",
            receiver_username="TestUser",
            amount=50,
            reason="Return payment",
            created_at=datetime(2024, 1, 14, 15, 45)
        )
    ]


class TestBalanceEmbed:
    """Test suite for balance embed creation."""
    
    def test_basic_balance_embed(self, sample_balance):
        """Test creating a basic balance embed."""
        embed = create_balance_embed(sample_balance)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "💰 Your Bytes Balance"
        assert embed.color == hikari.Color(0x3b82f6)
        assert len(embed.fields) >= 3  # Balance, received, sent
        
        # Check field values
        balance_field = next(f for f in embed.fields if "Balance" in f.name)
        assert "1,500" in balance_field.value
        
        received_field = next(f for f in embed.fields if "Received" in f.name)
        assert "3,000" in received_field.value
        
        sent_field = next(f for f in embed.fields if "Sent" in f.name)
        assert "1,500" in sent_field.value
    
    def test_balance_embed_with_daily_claim(self, sample_balance):
        """Test balance embed with daily claim information."""
        embed = create_balance_embed(
            sample_balance,
            daily_earned=30,
            streak=16,
            multiplier=4
        )
        
        assert isinstance(embed, hikari.Embed)
        assert len(embed.fields) >= 6  # Additional fields for daily info
        
        # Check for daily claim fields
        daily_field = next(f for f in embed.fields if "Daily Earned" in f.name)
        assert "30" in daily_field.value
        
        streak_field = next(f for f in embed.fields if "Streak" in f.name)
        assert "16" in streak_field.value
        assert "RARE" in streak_field.value  # 16 days = RARE tier
        
        multiplier_field = next(f for f in embed.fields if "Bonus" in f.name)
        assert "4x" in multiplier_field.value
    
    def test_balance_embed_with_existing_streak(self, sample_balance):
        """Test balance embed showing existing streak without daily claim."""
        embed = create_balance_embed(sample_balance)
        
        # Should show current streak from balance
        streak_field = next(f for f in embed.fields if "Streak" in f.name)
        assert "15" in streak_field.value
        assert "RARE" in streak_field.value
    
    def test_balance_embed_with_last_daily(self, sample_balance):
        """Test balance embed with last daily claim date."""
        embed = create_balance_embed(sample_balance)
        
        last_daily_field = next(f for f in embed.fields if "Last Daily" in f.name)
        assert "January 15, 2024" in last_daily_field.value


class TestStreakNames:
    """Test suite for streak name generation."""
    
    def test_streak_name_tiers(self):
        """Test all streak name tiers."""
        assert get_streak_name(0) == "BUILDING"
        assert get_streak_name(5) == "BUILDING"
        assert get_streak_name(7) == "COMMON"
        assert get_streak_name(10) == "COMMON"
        assert get_streak_name(14) == "RARE"
        assert get_streak_name(20) == "RARE"
        assert get_streak_name(30) == "EPIC"
        assert get_streak_name(45) == "EPIC"
        assert get_streak_name(60) == "LEGENDARY"
        assert get_streak_name(100) == "LEGENDARY"


class TestErrorEmbeds:
    """Test suite for error and status embeds."""
    
    def test_error_embed(self):
        """Test error embed creation."""
        message = "Something went wrong!"
        embed = create_error_embed(message)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "❌ Error"
        assert embed.description == message
        assert embed.color == hikari.Color(0xef4444)
        assert embed.timestamp is not None
    
    def test_success_embed(self):
        """Test success embed creation."""
        title = "Operation Successful"
        description = "Everything worked perfectly!"
        embed = create_success_embed(title, description)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == title
        assert embed.description == description
        assert embed.color == hikari.Color(0x22c55e)
        assert embed.timestamp is not None
    
    def test_warning_embed(self):
        """Test warning embed creation."""
        title = "Warning"
        description = "Be careful!"
        embed = create_warning_embed(title, description)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == title
        assert embed.description == description
        assert embed.color == hikari.Color(0xf59e0b)
        assert embed.timestamp is not None
    
    def test_info_embed(self):
        """Test info embed creation."""
        title = "Information"
        description = "Here's some info"
        embed = create_info_embed(title, description)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == title
        assert embed.description == description
        assert embed.color == hikari.Color(0x3b82f6)
        assert embed.timestamp is not None


class TestLeaderboardEmbed:
    """Test suite for leaderboard embed creation."""
    
    def test_leaderboard_embed_with_entries(self):
        """Test leaderboard embed with entries."""
        from smarter_dev.bot.services.models import LeaderboardEntry
        
        entries = [
            LeaderboardEntry(rank=1, user_id="111", balance=2000, total_received=3000),
            LeaderboardEntry(rank=2, user_id="222", balance=1500, total_received=2000),
            LeaderboardEntry(rank=3, user_id="333", balance=1000, total_received=1500)
        ]
        
        # Create display names mapping for testing
        user_display_names = {
            entry.user_id: f"User{entry.user_id}" for entry in entries
        }
        
        embed = create_leaderboard_embed(entries, "Test Guild", user_display_names)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "🏆 Bytes Leaderboard"
        assert embed.color == hikari.Color(0x3b82f6)
        assert "🥇" in embed.description  # Gold medal for first place
        assert "🥈" in embed.description  # Silver medal for second place
        assert "🥉" in embed.description  # Bronze medal for third place
        assert "2,000" in embed.description  # Balance formatting
        assert embed.footer.text == "Showing top 3 users"
    
    def test_leaderboard_embed_empty(self):
        """Test leaderboard embed with no entries."""
        embed = create_leaderboard_embed([], "Test Guild")
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "🏆 Bytes Leaderboard"
        assert "No leaderboard data available" in embed.description


class TestTransactionHistoryEmbed:
    """Test suite for transaction history embed creation."""
    
    def test_transaction_history_embed(self, sample_transactions):
        """Test transaction history embed with transactions."""
        user_id = "987654321"
        embed = create_transaction_history_embed(sample_transactions, user_id)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "📊 Your Transaction History"
        assert embed.color == hikari.Color(0x3b82f6)
        
        # Should show both sent and received transactions
        assert "📤" in embed.description  # Sent transaction icon
        assert "📥" in embed.description  # Received transaction icon
        assert "100" in embed.description  # Amount from first transaction
        assert "50" in embed.description   # Amount from second transaction
        assert "Test transfer" in embed.description  # Reason from first transaction
        
        assert embed.footer.text == "Showing 2 recent transactions"
    
    def test_transaction_history_embed_empty(self):
        """Test transaction history embed with no transactions."""
        embed = create_transaction_history_embed([], "987654321")
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "📊 Your Transaction History"
        assert "No transaction history found" in embed.description
    
    def test_transaction_history_embed_long_description(self, sample_transactions):
        """Test transaction history embed with description truncation."""
        # Create many transactions to test truncation
        long_transactions = sample_transactions * 100  # Repeat to exceed limit
        
        embed = create_transaction_history_embed(long_transactions, "987654321")
        
        # Description should be truncated
        assert len(embed.description) <= 4000
        if len(embed.description) == 4000:
            assert "truncated" in embed.description


class TestSquadListEmbed:
    """Test suite for squad list embed creation."""
    
    def test_squad_list_embed_with_squads(self):
        """Test squad list embed with squads."""
        from smarter_dev.bot.services.models import Squad
        
        squads = [
            Squad(
                id=uuid4(),
                guild_id="123456789",
                role_id="555666777",
                name="Alpha Squad",
                description="First squad",
                switch_cost=50,
                max_members=10,
                member_count=5,
                is_active=True,
                created_at=datetime.now(),
                updated_at=datetime.now()
            ),
            Squad(
                id=uuid4(),
                guild_id="123456789",
                role_id="777888999",
                name="Beta Squad",
                description="Second squad",
                switch_cost=100,
                max_members=None,
                member_count=8,
                is_active=True,
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
        ]
        
        embed = create_squad_list_embed(squads)
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "🏆 Available Squads"
        assert embed.color == hikari.Color(0x3b82f6)
        assert "2" in embed.description  # Number of squads
        assert len(embed.fields) == 2  # One field per squad
        
        # Check squad information
        alpha_field = next(f for f in embed.fields if "Alpha Squad" in f.name)
        assert "First squad" in alpha_field.value
        assert "👥 5/10" in alpha_field.value
        assert "50" in alpha_field.value
        
        beta_field = next(f for f in embed.fields if "Beta Squad" in f.name)
        assert "Second squad" in beta_field.value
        assert "👥 8 members" in beta_field.value  # No max members
        assert "100" in beta_field.value
    
    def test_squad_list_embed_with_current_squad(self):
        """Test squad list embed when user is in a squad."""
        from smarter_dev.bot.services.models import Squad
        
        squad_id = uuid4()
        squads = [
            Squad(
                id=squad_id,
                guild_id="123456789",
                role_id="555666777",
                name="Current Squad",
                description="User's squad",
                switch_cost=0,
                max_members=10,
                member_count=5,
                is_active=True,
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
        ]
        
        embed = create_squad_list_embed(squads, str(squad_id))
        
        # Should show checkmark for current squad
        current_field = next(f for f in embed.fields if "✅" in f.name)
        assert "Current Squad" in current_field.name
    
    def test_squad_list_embed_empty(self):
        """Test squad list embed with no squads."""
        embed = create_squad_list_embed([])
        
        assert isinstance(embed, hikari.Embed)
        assert embed.title == "🏆 Available Squads"
        assert "No squads have been created" in embed.description