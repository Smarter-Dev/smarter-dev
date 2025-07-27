"""Tests for Discord bot commands.

This module provides comprehensive tests for all bot commands, ensuring
they properly integrate with the service layer and handle errors gracefully.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime, date
from uuid import uuid4

from smarter_dev.bot.services.models import (
    BytesBalance, 
    DailyClaimResult, 
    TransferResult,
    BytesTransaction,
    Squad,
    UserSquadResponse,
    JoinSquadResult
)
from smarter_dev.bot.services.exceptions import (
    AlreadyClaimedError,
    InsufficientBalanceError,
    NotInSquadError,
    ServiceError
)


@pytest.fixture
def mock_context():
    """Create a mock lightbulb context."""
    ctx = Mock()
    ctx.guild_id = "123456789"
    ctx.user = Mock()
    ctx.user.id = "987654321"
    ctx.user.mention = "<@987654321>"
    ctx.respond = AsyncMock()
    
    # Mock guild and member access
    guild = Mock()
    guild.name = "Test Guild"
    guild.get_member = Mock(return_value=Mock(display_name="TestUser"))
    ctx.get_guild = Mock(return_value=guild)
    
    # Mock app with services
    ctx.app = Mock()
    ctx.app.d = Mock()
    
    return ctx


@pytest.fixture  
def mock_bytes_service():
    """Create a mock bytes service."""
    service = AsyncMock()
    service.get_balance.return_value = BytesBalance(
        guild_id="123456789",
        user_id="987654321", 
        balance=1000,
        total_received=2000,
        total_sent=1000,
        streak_count=5,
        last_daily=None,
        created_at=datetime.now(),
        updated_at=datetime.now()
    )
    return service


@pytest.fixture
def mock_squads_service():
    """Create a mock squads service."""
    service = AsyncMock()
    
    # Mock squad data
    mock_squad = Squad(
        id=uuid4(),
        guild_id="123456789",
        role_id="555666777",
        name="Test Squad",
        description="A test squad",
        switch_cost=50,
        max_members=None,
        member_count=5,
        is_active=True,
        created_at=datetime.now(),
        updated_at=datetime.now()
    )
    
    service.list_squads.return_value = [mock_squad]
    service.get_user_squad.return_value = UserSquadResponse(
        user_id="987654321",
        squad=None,
        member_since=None
    )
    
    return service


class TestBytesCommands:
    """Test suite for bytes economy commands."""
    
    async def test_balance_command_success(self, mock_context, mock_bytes_service):
        """Test successful balance command without auto-claiming."""
        # Setup context with proper service structure (both primary and fallback)
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }
        
        # Import and test command function directly
        from smarter_dev.bot.plugins.bytes import balance_command
        
        await balance_command(mock_context)
        
        # Verify service calls - only get_balance, no daily claim
        mock_bytes_service.get_balance.assert_called_once_with("123456789", "987654321")
        mock_bytes_service.claim_daily.assert_not_called()
        
        # Verify response shows balance only
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        embed = kwargs["embed"]
        assert "Your Bytes Balance" in embed.title
    
    
    async def test_send_command_success(self, mock_context, mock_bytes_service):
        """Test successful send command."""
        mock_context.app.d.bytes_service = mock_bytes_service
        
        # Mock successful transfer
        mock_transaction = BytesTransaction(
            id=uuid4(),
            guild_id="123456789",
            giver_id="987654321",
            giver_username="TestUser",
            receiver_id="111222333",
            receiver_username="OtherUser",
            amount=100,
            reason="Test transfer",
            created_at=datetime.now()
        )
        
        mock_bytes_service.transfer_bytes.return_value = TransferResult(
            success=True,
            transaction=mock_transaction,
            new_giver_balance=900
        )
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }
        
        # Mock options for the send command
        mock_context.options = Mock()
        mock_context.options.user = Mock(id="111222333", mention="<@111222333>")
        mock_context.options.amount = 100
        mock_context.options.reason = "Test transfer"
        
        # Mock guild member lookup
        member = Mock()
        mock_context.get_guild().get_member.return_value = member
        
        from smarter_dev.bot.plugins.bytes import send_command
        
        await send_command(mock_context)
        
        # Verify service call
        mock_bytes_service.transfer_bytes.assert_called_once()
        
        # Verify response
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        embed = kwargs["embed"]
        assert "Bytes Sent!" in embed.title
    
    async def test_send_command_insufficient_balance(self, mock_context, mock_bytes_service):
        """Test send command with insufficient balance."""
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }
        
        # Mock options for the send command
        mock_context.options = Mock()
        mock_context.options.user = Mock(id="111222333", mention="<@111222333>")
        mock_context.options.amount = 500
        mock_context.options.reason = None
        
        # Mock guild member lookup
        member = Mock()
        mock_context.get_guild().get_member.return_value = member
        
        # Mock insufficient balance error
        mock_bytes_service.transfer_bytes.side_effect = InsufficientBalanceError(
            required=500,
            available=100,
            operation="transfer"
        )
        
        from smarter_dev.bot.plugins.bytes import send_command
        
        await send_command(mock_context)
        
        # Verify error response
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        embed = kwargs["embed"]
        assert "Insufficient balance" in embed.description
    
    async def test_leaderboard_command_success(self, mock_context, mock_bytes_service):
        """Test successful leaderboard command."""
        mock_context.app.d.bytes_service = mock_bytes_service
        
        # Mock leaderboard entries
        from smarter_dev.bot.services.models import LeaderboardEntry
        entries = [
            LeaderboardEntry(rank=1, user_id="111", balance=2000, total_received=3000),
            LeaderboardEntry(rank=2, user_id="222", balance=1500, total_received=2000),
            LeaderboardEntry(rank=3, user_id="333", balance=1000, total_received=1500)
        ]
        mock_bytes_service.get_leaderboard.return_value = entries
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }
        
        # Mock options for the leaderboard command
        mock_context.options = Mock()
        mock_context.options.limit = 10
        
        from smarter_dev.bot.plugins.bytes import leaderboard_command
        
        await leaderboard_command(mock_context)
        
        # Verify service call
        mock_bytes_service.get_leaderboard.assert_called_once_with("123456789", 10)
        
        # Verify response
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        embed = kwargs["embed"]
        assert "Leaderboard" in embed.title
    
    async def test_history_command_success(self, mock_context, mock_bytes_service):
        """Test successful history command."""
        mock_context.app.d.bytes_service = mock_bytes_service
        
        # Mock transaction history
        transactions = [
            BytesTransaction(
                id=uuid4(),
                guild_id="123456789",
                giver_id="987654321",
                giver_username="TestUser",
                receiver_id="111222333",
                receiver_username="OtherUser",
                amount=50,
                reason="Test",
                created_at=datetime.now()
            )
        ]
        mock_bytes_service.get_transaction_history.return_value = transactions
        
        # Setup context with proper service structure  
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }
        
        # Mock options for the history command
        mock_context.options = Mock()
        mock_context.options.limit = 10
        
        from smarter_dev.bot.plugins.bytes import history_command
        
        await history_command(mock_context)
        
        # Verify service call
        mock_bytes_service.get_transaction_history.assert_called_once_with(
            "123456789", user_id="987654321", limit=10
        )
        
        # Verify response
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL


class TestSquadCommands:
    """Test suite for squad management commands."""
    
    async def test_list_command_success(self, mock_context, mock_squads_service):
        """Test successful squad list command."""
        mock_context.app.d.squads_service = mock_squads_service
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'squads_service': mock_squads_service,
            '_services': {'squads_service': mock_squads_service}
        }
        
        from smarter_dev.bot.plugins.squads import list_command
        
        await list_command(mock_context)
        
        # Verify service calls
        mock_squads_service.list_squads.assert_called_once_with("123456789")
        mock_squads_service.get_user_squad.assert_called_once_with("123456789", "987654321")
        
        # Verify response
        mock_context.respond.assert_called_once()
        embed = mock_context.respond.call_args[1]["embed"]
        assert "Available Squads" in embed.title
    
    async def test_join_command_success(self, mock_context, mock_squads_service, mock_bytes_service):
        """Test successful squad join command."""
        mock_context.app.d.squads_service = mock_squads_service
        mock_context.app.d.bytes_service = mock_bytes_service
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'squads_service': mock_squads_service,
            'bytes_service': mock_bytes_service,
            '_services': {
                'squads_service': mock_squads_service,
                'bytes_service': mock_bytes_service
            }
        }
        
        from smarter_dev.bot.plugins.squads import join_command
        
        await join_command(mock_context)
        
        # Verify service calls
        mock_squads_service.list_squads.assert_called_once_with("123456789")
        mock_bytes_service.get_balance.assert_called_once_with("123456789", "987654321")
        mock_squads_service.get_user_squad.assert_called_once_with("123456789", "987654321")
        
        # Verify response with interactive components
        mock_context.respond.assert_called_once()
        call_kwargs = mock_context.respond.call_args[1]
        assert "components" in call_kwargs
        assert call_kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
    
    async def test_leave_command_success(self, mock_context, mock_squads_service):
        """Test successful squad leave command."""
        mock_context.app.d.squads_service = mock_squads_service
        
        # Mock user in squad
        mock_squad = Squad(
            id=uuid4(),
            guild_id="123456789",
            role_id="555666777",
            name="Test Squad",
            description="A test squad",
            switch_cost=50,
            max_members=None,
            member_count=5,
            is_active=True,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        
        mock_squads_service.get_user_squad.return_value = UserSquadResponse(
            user_id="987654321",
            squad=mock_squad,
            member_since=datetime.now()
        )
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'squads_service': mock_squads_service,
            '_services': {'squads_service': mock_squads_service}
        }
        
        from smarter_dev.bot.plugins.squads import leave_command
        
        await leave_command(mock_context)
        
        # Verify service calls
        mock_squads_service.get_user_squad.assert_called_once_with("123456789", "987654321")
        mock_squads_service.leave_squad.assert_called_once_with("123456789", "987654321")
        
        # Verify response
        mock_context.respond.assert_called_once()
        call_kwargs = mock_context.respond.call_args[1]
        assert call_kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        embed = call_kwargs["embed"]
        assert "Squad Left" in embed.title
    
    async def test_leave_command_not_in_squad(self, mock_context, mock_squads_service):
        """Test leave command when user not in any squad."""
        mock_context.app.d.squads_service = mock_squads_service
        
        # Mock user not in squad
        mock_squads_service.get_user_squad.return_value = UserSquadResponse(
            user_id="987654321",
            squad=None,
            member_since=None
        )
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'squads_service': mock_squads_service,
            '_services': {'squads_service': mock_squads_service}
        }
        
        from smarter_dev.bot.plugins.squads import leave_command
        
        await leave_command(mock_context)
        
        # Verify error response
        mock_context.respond.assert_called_once()
        call_kwargs = mock_context.respond.call_args[1]
        assert call_kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        embed = call_kwargs["embed"]
        assert "not currently in any squad" in embed.description
    
    async def test_info_command_success(self, mock_context, mock_squads_service):
        """Test successful squad info command."""
        mock_context.app.d.squads_service = mock_squads_service
        
        # Mock user in squad
        mock_squad = Squad(
            id=uuid4(),
            guild_id="123456789",
            role_id="555666777",
            name="Test Squad",
            description="A test squad",
            switch_cost=50,
            max_members=10,
            member_count=5,
            is_active=True,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        
        mock_squads_service.get_user_squad.return_value = UserSquadResponse(
            user_id="987654321",
            squad=mock_squad,
            member_since=datetime.now()
        )
        
        # Mock squad members
        from smarter_dev.bot.services.models import SquadMember
        members = [
            SquadMember(user_id="987654321", username="TestUser", joined_at=datetime.now()),
            SquadMember(user_id="111222333", username="OtherUser", joined_at=datetime.now())
        ]
        mock_squads_service.get_squad_members.return_value = members
        
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'squads_service': mock_squads_service,
            '_services': {'squads_service': mock_squads_service}
        }
        
        # Mock options for info command
        mock_context.options = Mock()
        mock_context.options.user = Mock(id="987654321")
        
        from smarter_dev.bot.plugins.squads import info_command
        
        await info_command(mock_context)
        
        # Verify service calls
        mock_squads_service.get_user_squad.assert_called_once_with("123456789", "987654321")
        mock_squads_service.get_squad_members.assert_called_once_with("123456789", mock_squad.id)
        
        # Verify response
        mock_context.respond.assert_called_once()
        call_kwargs = mock_context.respond.call_args[1]
        assert call_kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        embed = call_kwargs["embed"]
        assert "Test Squad" in embed.title


class TestCommandErrorHandling:
    """Test suite for command error handling."""
    
    async def test_bytes_service_unavailable(self, mock_context):
        """Test command behavior when bytes service is unavailable."""
        # Setup context with no service
        mock_context.bot = Mock()
        mock_context.bot.d = {'_services': {}}
        
        from smarter_dev.bot.plugins.bytes import balance_command
        
        await balance_command(mock_context)
        
        # Verify error response
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        embed = kwargs["embed"]
        assert "not initialized" in embed.description
    
    async def test_service_error_handling(self, mock_context, mock_bytes_service):
        """Test command behavior when service raises ServiceError."""
        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }
        
        # Mock service error
        mock_bytes_service.get_balance.side_effect = ServiceError("Service unavailable")
        
        from smarter_dev.bot.plugins.bytes import balance_command
        
        await balance_command(mock_context)
        
        # Verify error response
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        embed = kwargs["embed"]
        assert "Failed to retrieve balance" in embed.description