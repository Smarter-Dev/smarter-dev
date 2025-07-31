"""Comprehensive integration tests for the complete bytes system."""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, Mock

from bot.services.bytes_service import BytesService
from bot.plugins.bytes_commands import BalanceCommand, SendCommand, LeaderboardCommand
from bot.views.bytes_views import TransferConfirmView, DailyClaimView
from shared.types import StreakMultiplier
from bot.errors import InsufficientBytesError


@pytest.fixture
def mock_api():
    """Create mock API client for bytes service."""
    api = AsyncMock()
    return api


@pytest.fixture
def bytes_service(mock_api):
    """Create bytes service with mock API."""
    return BytesService(mock_api)


@pytest.fixture
def mock_lightbulb_context():
    """Create mock Lightbulb context."""
    ctx = Mock()
    ctx.guild_id = "123456789012345678"
    ctx.author = Mock()
    ctx.author.id = "987654321098765432"
    ctx.author.username = "TestUser"
    ctx.respond = AsyncMock()
    ctx.client = Mock()
    ctx.client.app = Mock()
    ctx.client.app.cache = Mock()
    return ctx


class TestBytesSystemIntegration:
    """Integration tests for the complete bytes system."""

    @pytest.mark.asyncio
    async def test_complete_daily_award_flow(self, bytes_service, mock_api):
        """Test complete daily award flow with streak calculations."""
        guild_id = "123456789012345678"
        user_id = "987654321098765432"
        username = "TestUser"

        # Simulate user with existing streak
        mock_api.award_daily_bytes.return_value = {
            "amount_awarded": 40,  # 10 base * 4 multiplier for SHORT streak
            "streak_count": 16,
            "new_balance": 540,
        }

        amount, streak, multiplier = await bytes_service.award_daily(
            guild_id, user_id, username
        )

        assert amount == 40
        assert streak == 16
        assert multiplier == StreakMultiplier.SHORT
        assert multiplier.multiplier == 4
        mock_api.award_daily_bytes.assert_called_once_with(guild_id, user_id, username)

    @pytest.mark.asyncio
    async def test_transfer_with_confirmation_flow(self, bytes_service, mock_api):
        """Test transfer flow with validation and confirmation."""
        guild_id = "123456789012345678"
        giver_id = "111111111111111111"
        receiver_id = "222222222222222222"
        amount = 150  # Large amount requiring confirmation

        # Setup API mocks
        mock_api.get_bytes_config.return_value = {"max_transfer": 1000}
        mock_api.get_bytes_balance.return_value = {"balance": 500}
        mock_api.transfer_bytes.return_value = {
            "giver_new_balance": 350,
            "receiver_new_balance": 250,
            "giver_total_sent": 200,
            "receiver_total_received": 150,
        }

        # Test validation passes
        result = await bytes_service.transfer(
            guild_id=guild_id,
            giver_id=giver_id,
            giver_username="Giver",
            receiver_id=receiver_id,
            receiver_username="Receiver",
            amount=amount,
            reason="Integration test",
        )

        assert result["giver_new_balance"] == 350
        assert result["receiver_new_balance"] == 250

        # Verify API calls
        mock_api.get_bytes_config.assert_called_with(guild_id)
        mock_api.get_bytes_balance.assert_called_with(guild_id, giver_id)
        mock_api.transfer_bytes.assert_called_once()

    @pytest.mark.asyncio
    async def test_transfer_insufficient_funds_error(self, bytes_service, mock_api):
        """Test transfer with insufficient funds raises proper error."""
        guild_id = "123456789012345678"
        giver_id = "111111111111111111"
        receiver_id = "222222222222222222"
        amount = 600  # More than balance

        # Setup API mocks
        mock_api.get_bytes_config.return_value = {"max_transfer": 1000}
        mock_api.get_bytes_balance.return_value = {"balance": 500}  # Insufficient

        with pytest.raises(InsufficientBytesError) as exc_info:
            await bytes_service.transfer(
                guild_id=guild_id,
                giver_id=giver_id,
                giver_username="Giver",
                receiver_id=receiver_id,
                receiver_username="Receiver",
                amount=amount,
            )

        assert exc_info.value.current == 500
        assert exc_info.value.required == 600
        assert "need **600** bytes but only have **500**" in exc_info.value.user_message

    @pytest.mark.asyncio
    async def test_leaderboard_with_caching(self, bytes_service, mock_api):
        """Test leaderboard retrieval with caching behavior."""
        guild_id = "123456789012345678"
        limit = 10

        leaderboard_data = [
            {"user_id": "111", "balance": 1000, "total_received": 1200},
            {"user_id": "222", "balance": 800, "total_received": 900},
            {"user_id": "333", "balance": 600, "total_received": 700},
        ]
        mock_api.get_bytes_leaderboard.return_value = leaderboard_data

        # First call
        result1 = await bytes_service.get_leaderboard(guild_id, limit)

        # Second call (should hit cache in real implementation)
        result2 = await bytes_service.get_leaderboard(guild_id, limit)

        assert result1 == leaderboard_data
        assert result2 == leaderboard_data
        assert len(result1) == 3
        assert result1[0]["balance"] == 1000

    @pytest.mark.asyncio
    async def test_role_rewards_calculation(self, bytes_service, mock_api):
        """Test role reward threshold checking."""
        guild_id = "123456789012345678"
        user_id = "987654321098765432"
        user_balance = 750

        config_data = {
            "role_rewards": {
                "role_bronze": 100,
                "role_silver": 500,
                "role_gold": 1000,
                "role_platinum": 2000,
            }
        }
        mock_api.get_bytes_config.return_value = config_data

        earned_roles = await bytes_service.check_role_rewards(
            guild_id, user_id, user_balance
        )

        # Should earn bronze and silver, but not gold or platinum
        assert "role_bronze" in earned_roles
        assert "role_silver" in earned_roles
        assert "role_gold" not in earned_roles
        assert "role_platinum" not in earned_roles

    @pytest.mark.asyncio
    async def test_streak_multiplier_progression(self, bytes_service):
        """Test streak multiplier calculations across all tiers."""
        test_cases = [
            (0, 10, 10, StreakMultiplier.NONE),
            (7, 10, 10, StreakMultiplier.NONE),  # Below CHAR threshold
            (8, 10, 20, StreakMultiplier.CHAR),   # Exactly CHAR threshold
            (15, 10, 20, StreakMultiplier.CHAR),  # Still CHAR
            (16, 10, 40, StreakMultiplier.SHORT), # Exactly SHORT threshold
            (31, 10, 40, StreakMultiplier.SHORT), # Still SHORT
            (32, 10, 160, StreakMultiplier.INT),  # Exactly INT threshold
            (63, 10, 160, StreakMultiplier.INT),  # Still INT
            (64, 10, 2560, StreakMultiplier.LONG), # Exactly LONG threshold
            (100, 10, 2560, StreakMultiplier.LONG), # Still LONG
        ]

        for (
            streak_days,
            base_amount,
            expected_amount,
            expected_multiplier,
        ) in test_cases:
            amount, multiplier = bytes_service.calculate_daily_amount(
                base_amount, streak_days
            )
            assert (
                amount == expected_amount
            ), f"Streak {streak_days}: expected {expected_amount}, got {amount}"
            assert (
                multiplier == expected_multiplier
            ), f"Streak {streak_days}: expected {expected_multiplier}, got {multiplier}"


class TestBytesCommandIntegration:
    """Integration tests for Discord bot commands."""

    @pytest.mark.asyncio
    async def test_balance_command_flow(self, mock_lightbulb_context):
        """Test complete balance command flow."""

        # Mock the global bytes_service
        with patch("bot.plugins.bytes_commands.bytes_service") as mock_service:
            mock_service.check_balance.return_value = {
                "balance": 500,
                "total_received": 600,
                "total_sent": 100,
                "streak_count": 7,
                "daily_available": True,
            }

            command = BalanceCommand()
            await command.invoke(mock_lightbulb_context)

            # Verify service call
            mock_service.check_balance.assert_called_once_with(
                "123456789012345678", "987654321098765432", "TestUser"
            )

            # Verify response
            mock_lightbulb_context.respond.assert_called_once()
            call_args = mock_lightbulb_context.respond.call_args
            embed = call_args[1]["embed"]
            assert embed.title == "💰 Bytes Balance"

    @pytest.mark.asyncio
    async def test_send_command_small_amount(self, mock_lightbulb_context):
        """Test send command with small amount (no confirmation needed)."""

        # Mock receiver user
        receiver = Mock()
        receiver.id = "333333333333333333"
        receiver.username = "Receiver"
        receiver.mention = "<@333333333333333333>"
        receiver.is_bot = False

        with patch("bot.plugins.bytes_commands.bytes_service") as mock_service:
            mock_service.transfer.return_value = {
                "giver_balance": 450,
                "giver_total_sent": 50,
            }

            command = SendCommand()
            command.user = receiver
            command.amount = 50  # Small amount, no confirmation needed
            command.reason = "Test transfer"

            await command.invoke(mock_lightbulb_context)

            # Verify transfer was called
            mock_service.transfer.assert_called_once_with(
                guild_id="123456789012345678",
                giver_id="987654321098765432",
                giver_username="TestUser",
                receiver_id="333333333333333333",
                receiver_username="Receiver",
                amount=50,
                reason="Test transfer",
            )

            # Verify success response
            mock_lightbulb_context.respond.assert_called_once()

    @pytest.mark.asyncio
    async def test_leaderboard_command_with_users(self, mock_lightbulb_context):
        """Test leaderboard command with user data."""

        # Mock cached users
        mock_user1 = Mock()
        mock_user1.username = "TopUser"
        mock_user2 = Mock()
        mock_user2.username = "SecondUser"

        mock_lightbulb_context.client.app.cache.get_user.side_effect = [
            mock_user1,
            mock_user2,
            None,  # Third user not in cache
        ]

        with patch("bot.plugins.bytes_commands.bytes_service") as mock_service:
            mock_service.get_leaderboard.return_value = [
                {"user_id": "111", "balance": 1000},
                {"user_id": "222", "balance": 800},
                {"user_id": "333", "balance": 600},
            ]

            command = LeaderboardCommand()
            command.limit = 10

            await command.invoke(mock_lightbulb_context)

            # Verify service call
            mock_service.get_leaderboard.assert_called_once_with(
                "123456789012345678", 10
            )

            # Verify response
            mock_lightbulb_context.respond.assert_called_once()


class TestBytesViewsIntegration:
    """Integration tests for Discord UI views."""

    @pytest.mark.asyncio
    async def test_transfer_confirmation_view_creation(self):
        """Test transfer confirmation view component building."""
        giver = Mock()
        giver.id = "111111111111111111"

        receiver = Mock()
        receiver.id = "222222222222222222"
        receiver.mention = "<@222222222222222222>"

        view = TransferConfirmView(
            giver=giver,
            receiver=receiver,
            amount=150,
            reason="Test transfer",
            timeout=60,
        )

        components = view.build_components()

        assert len(components) == 1  # One action row
        row = components[0]

        # Should have confirm and cancel buttons
        assert len(row._components) == 2

        # Check button properties
        buttons = row._components
        confirm_button = buttons[0]
        cancel_button = buttons[1]

        assert confirm_button._label == "Confirm Transfer"
        assert cancel_button._label == "Cancel"

    @pytest.mark.asyncio
    async def test_daily_claim_view_creation(self):
        """Test daily claim view component building."""
        user = Mock()
        user.id = "111111111111111111"

        view = DailyClaimView(
            user=user, amount=40, multiplier_display="4x (SHORT streak)", timeout=30
        )

        components = view.build_components()

        assert len(components) == 1  # One action row
        row = components[0]

        # Should have one claim button
        assert len(row._components) == 1

        claim_button = row._components[0]
        assert claim_button._label == "Claim 40 Bytes"


class TestBytesSystemPerformance:
    """Performance and edge case tests for the bytes system."""

    @pytest.mark.asyncio
    async def test_concurrent_transfer_validation(self, bytes_service, mock_api):
        """Test that concurrent transfers are handled safely."""
        guild_id = "123456789012345678"
        giver_id = "111111111111111111"

        # Setup API mocks
        mock_api.get_bytes_config.return_value = {"max_transfer": 1000}
        mock_api.get_bytes_balance.return_value = {"balance": 500}
        mock_api.transfer_bytes.return_value = {
            "giver_new_balance": 400,
            "receiver_new_balance": 200,
        }

        # Simulate concurrent transfers
        tasks = []
        for i in range(3):
            task = bytes_service.transfer(
                guild_id=guild_id,
                giver_id=giver_id,
                giver_username="Giver",
                receiver_id=f"22222222222222222{i}",
                receiver_username=f"Receiver{i}",
                amount=100,
            )
            tasks.append(task)

        # All should complete (API layer should handle concurrency)
        results = await asyncio.gather(*tasks)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_edge_case_zero_balance_transfer(self, bytes_service, mock_api):
        """Test transfer attempt with zero balance."""
        guild_id = "123456789012345678"
        giver_id = "111111111111111111"

        # Setup API mocks for zero balance
        mock_api.get_bytes_config.return_value = {"max_transfer": 1000}
        mock_api.get_bytes_balance.return_value = {"balance": 0}

        with pytest.raises(InsufficientBytesError):
            await bytes_service.transfer(
                guild_id=guild_id,
                giver_id=giver_id,
                giver_username="Giver",
                receiver_id="222222222222222222",
                receiver_username="Receiver",
                amount=1,  # Even 1 byte should fail
            )

    @pytest.mark.asyncio
    async def test_config_caching_behavior(self, bytes_service, mock_api):
        """Test configuration caching reduces API calls."""
        guild_id = "123456789012345678"

        config_data = {
            "starting_balance": 100,
            "daily_amount": 10,
            "max_transfer": 1000,
        }
        mock_api.get_bytes_config.return_value = config_data

        # Multiple calls should use cache
        config1 = await bytes_service.get_config(guild_id)
        config2 = await bytes_service.get_config(guild_id)
        config3 = await bytes_service.get_config(guild_id)

        assert config1 == config_data
        assert config2 == config_data
        assert config3 == config_data

        # Should only call API once due to caching
        mock_api.get_bytes_config.assert_called_once_with(guild_id)

    @pytest.mark.asyncio
    async def test_large_leaderboard_performance(self, bytes_service, mock_api):
        """Test leaderboard performance with large dataset."""
        guild_id = "123456789012345678"

        # Generate large leaderboard data
        large_leaderboard = []
        for i in range(100):
            large_leaderboard.append(
                {
                    "user_id": f"{i:018d}",
                    "balance": 1000 - i,
                    "total_received": 1200 - i,
                }
            )

        mock_api.get_bytes_leaderboard.return_value = large_leaderboard

        # Request should handle large dataset
        result = await bytes_service.get_leaderboard(guild_id, 50)

        assert len(result) == 100  # All data returned
        assert result[0]["balance"] == 1000  # Highest balance first
        assert result[99]["balance"] == 901  # Lowest balance last


class TestBytesSystemErrorHandling:
    """Test error handling across the bytes system."""

    @pytest.mark.asyncio
    async def test_api_timeout_handling(self, bytes_service, mock_api):
        """Test handling of API timeouts."""
        guild_id = "123456789012345678"
        user_id = "987654321098765432"

        # Simulate API timeout
        mock_api.get_bytes_balance.side_effect = asyncio.TimeoutError("API timeout")

        # Should fall back to defaults for new users
        result = await bytes_service.check_balance(guild_id, user_id, "TestUser")

        # Should return default values
        assert result["balance"] == 100  # Default starting balance
        assert result["total_received"] == 0
        assert result["total_sent"] == 0
        assert result["streak_count"] == 0
        assert result["daily_available"] is True

    @pytest.mark.asyncio
    async def test_config_api_error_fallback(self, bytes_service, mock_api):
        """Test config fallback when API fails."""
        guild_id = "123456789012345678"

        # Simulate API error
        mock_api.get_bytes_config.side_effect = Exception("Database error")

        config = await bytes_service.get_config(guild_id)

        # Should return default configuration
        assert config["starting_balance"] == 100
        assert config["daily_amount"] == 10
        assert config["max_transfer"] == 1000
        assert config["cooldown_hours"] == 24
        assert config["role_rewards"] == {}

    @pytest.mark.asyncio
    async def test_leaderboard_api_error_recovery(self, bytes_service, mock_api):
        """Test leaderboard error recovery."""
        guild_id = "123456789012345678"

        # Simulate API error
        mock_api.get_bytes_leaderboard.side_effect = Exception("Database error")

        result = await bytes_service.get_leaderboard(guild_id, 10)

        # Should return empty list instead of crashing
        assert result == []
