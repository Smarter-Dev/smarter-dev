"""Tests for Discord bot commands.

This module provides comprehensive tests for all bot commands, ensuring
they properly integrate with the service layer and handle errors gracefully.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
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
    ctx.user.display_name = "TestUser"
    ctx.user.username = "testuser"
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


@pytest.fixture
def mock_image_generator():
    """Create a mock image generator that returns a Mock file for each embed method."""
    generator = Mock()
    generator.create_balance_embed.return_value = Mock(name="balance_image")
    generator.create_error_embed.return_value = Mock(name="error_image")
    generator.create_success_embed.return_value = Mock(name="success_image")
    generator.create_leaderboard_embed.return_value = Mock(name="leaderboard_image")
    generator.create_history_embed.return_value = Mock(name="history_image")
    generator.create_config_embed.return_value = Mock(name="config_image")
    generator.create_squad_list_embed.return_value = Mock(name="squad_list_image")
    generator.create_squad_info_embed.return_value = Mock(name="squad_info_image")
    generator.create_squad_join_selector_embed.return_value = Mock(name="squad_join_image")
    generator.create_simple_embed.return_value = Mock(name="simple_image")
    return generator


class TestBytesCommands:
    """Test suite for bytes economy commands."""

    async def test_balance_command_success(self, mock_context, mock_bytes_service, mock_image_generator):
        """Test successful balance command without auto-claiming."""
        # Setup context with proper service structure (both primary and fallback)
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'bytes_service': mock_bytes_service,
            '_services': {'bytes_service': mock_bytes_service}
        }

        # Import and test command function directly
        from smarter_dev.bot.plugins.bytes import balance_command
        import smarter_dev.bot.views.balance_views as balance_views_module

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            with patch.object(balance_views_module, 'BalanceShareView') as MockShareView:
                MockShareView.return_value.build_components.return_value = []
                await balance_command(mock_context)

        # Verify service calls - only get_balance, no daily claim
        mock_bytes_service.get_balance.assert_called_once_with("123456789", "987654321")
        mock_bytes_service.claim_daily.assert_not_called()

        # Verify response uses image attachment (not embed)
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert "attachment" in kwargs
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL


    async def test_send_command_success(self, mock_context, mock_bytes_service, mock_image_generator):
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
        mock_context.options.user = Mock(id="111222333", mention="<@111222333>", display_name="OtherUser", username="otheruser")
        mock_context.options.amount = 100
        mock_context.options.reason = "Test transfer"

        # Mock guild member lookup
        member = Mock(display_name="OtherUser")
        mock_context.get_guild().get_member.return_value = member

        from smarter_dev.bot.plugins.bytes import send_command

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            await send_command(mock_context)

        # Verify service call
        mock_bytes_service.transfer_bytes.assert_called_once()

        # Verify response uses image attachment
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert "attachment" in kwargs

    async def test_send_command_insufficient_balance(self, mock_context, mock_bytes_service, mock_image_generator):
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

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            await send_command(mock_context)

        # Verify error response uses image attachment with ephemeral flag
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        assert "attachment" in kwargs
        # Verify error embed was created with insufficient balance message
        mock_image_generator.create_error_embed.assert_called_once()
        error_message = mock_image_generator.create_error_embed.call_args[0][0]
        assert "Insufficient balance" in error_message

    async def test_leaderboard_command_success(self, mock_context, mock_bytes_service, mock_image_generator):
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
        import smarter_dev.bot.views.leaderboard_views as leaderboard_views_module

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            with patch.object(leaderboard_views_module, 'LeaderboardShareView') as MockShareView:
                MockShareView.return_value.build_components.return_value = []
                await leaderboard_command(mock_context)

        # Verify service call
        mock_bytes_service.get_leaderboard.assert_called_once_with("123456789", 10)

        # Verify response uses image attachment (for <= 10 entries)
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert "attachment" in kwargs

    async def test_history_command_success(self, mock_context, mock_bytes_service, mock_image_generator):
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
        import smarter_dev.bot.views.history_views as history_views_module

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            with patch.object(history_views_module, 'HistoryShareView') as MockShareView:
                MockShareView.return_value.build_components.return_value = []
                await history_command(mock_context)

        # Verify service call
        mock_bytes_service.get_transaction_history.assert_called_once_with(
            "123456789", user_id="987654321", limit=10
        )

        # Verify response uses image attachment with ephemeral flag
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL


class TestSquadCommands:
    """Test suite for squad management commands."""

    async def test_list_command_success(self, mock_context, mock_squads_service, mock_image_generator):
        """Test successful squad list command."""
        mock_context.app.d.squads_service = mock_squads_service

        # Setup context with proper service structure
        mock_context.bot = Mock()
        mock_context.bot.d = {
            'squads_service': mock_squads_service,
            '_services': {'squads_service': mock_squads_service}
        }

        # Mock guild roles
        mock_role = Mock()
        mock_role.id = 555666777
        mock_role.color = Mock()
        mock_context.get_guild().get_roles.return_value = {mock_role.id: mock_role}

        # Mock _check_active_campaign
        mock_squads_service._check_active_campaign = AsyncMock(return_value=False)

        from smarter_dev.bot.plugins.squads import list_command

        with patch('smarter_dev.bot.plugins.squads.get_generator', return_value=mock_image_generator):
            with patch('smarter_dev.bot.plugins.squads.SquadListShareView') as MockShareView:
                MockShareView.return_value.build_components.return_value = []
                await list_command(mock_context)

        # Verify service calls
        mock_squads_service.list_squads.assert_called_once_with("123456789")
        mock_squads_service.get_user_squad.assert_called_once_with("123456789", "987654321")

        # Verify response uses image attachment
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert "attachment" in kwargs

    async def test_join_command_success(self, mock_context, mock_squads_service, mock_bytes_service, mock_image_generator):
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

        # Mock options - no squad name given (interactive mode)
        mock_context.options = Mock()
        mock_context.options.squad = None

        # Mock _check_active_campaign
        mock_squads_service._check_active_campaign = AsyncMock(return_value=False)

        # The join command defers with ctx.respond(DEFERRED_MESSAGE_CREATE), then edits
        mock_context.edit_last_response = AsyncMock()

        from smarter_dev.bot.plugins.squads import join_command

        with patch('smarter_dev.bot.plugins.squads.get_generator', return_value=mock_image_generator):
            with patch('smarter_dev.bot.plugins.squads.SquadSelectView') as MockView:
                MockView.return_value.build.return_value = []
                MockView.return_value.start = Mock()
                await join_command(mock_context)

        # Verify the command deferred the response first
        mock_context.respond.assert_called_once()

        # Verify service calls for interactive mode
        mock_squads_service.list_squads.assert_called()
        mock_bytes_service.get_balance.assert_called()
        mock_squads_service.get_user_squad.assert_called()

    async def test_info_command_success(self, mock_context, mock_squads_service, mock_image_generator):
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

        with patch('smarter_dev.bot.plugins.squads.get_generator', return_value=mock_image_generator):
            await info_command(mock_context)

        # Verify service calls
        mock_squads_service.get_user_squad.assert_called_once_with("123456789", "987654321")
        mock_squads_service.get_squad_members.assert_called_once_with("123456789", mock_squad.id)

        # Verify response uses image attachment with ephemeral flag
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        assert "attachment" in kwargs


class TestCommandErrorHandling:
    """Test suite for command error handling."""

    async def test_bytes_service_unavailable(self, mock_context, mock_image_generator):
        """Test command behavior when bytes service is unavailable."""
        # Setup context with no service
        mock_context.bot = Mock()
        mock_context.bot.d = {'_services': {}}

        from smarter_dev.bot.plugins.bytes import balance_command

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            await balance_command(mock_context)

        # Verify error response uses image attachment
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert kwargs["flags"] == pytest.importorskip("hikari").MessageFlag.EPHEMERAL
        assert "attachment" in kwargs
        # Verify error message mentions not initialized
        mock_image_generator.create_error_embed.assert_called_once()
        error_message = mock_image_generator.create_error_embed.call_args[0][0]
        assert "not initialized" in error_message

    async def test_service_error_handling(self, mock_context, mock_bytes_service, mock_image_generator):
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

        with patch('smarter_dev.bot.plugins.bytes.get_generator', return_value=mock_image_generator):
            await balance_command(mock_context)

        # Verify error response uses image attachment
        mock_context.respond.assert_called_once()
        args, kwargs = mock_context.respond.call_args
        assert "attachment" in kwargs
        # Verify error message mentions failure
        mock_image_generator.create_error_embed.assert_called_once()
        error_message = mock_image_generator.create_error_embed.call_args[0][0]
        assert "Failed to retrieve balance" in error_message
