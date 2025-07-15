"""Tests for Discord bot interactive views.

This module tests the interactive components used by the bot,
including squad selection views and confirmation dialogs.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock
from datetime import datetime
from uuid import uuid4

from smarter_dev.bot.views.squad_views import SquadSelectView, SquadConfirmView
from smarter_dev.bot.services.models import Squad, JoinSquadResult


@pytest.fixture
def mock_squads():
    """Create mock squad data."""
    return [
        Squad(
            id=uuid4(),
            guild_id="123456789",
            role_id="555666777",
            name="Squad Alpha",
            description="First squad",
            switch_cost=50,
            max_members=10,
            member_count=5,
            is_active=True,
            is_full=False,
            created_at=datetime.now(),
            updated_at=datetime.now()
        ),
        Squad(
            id=uuid4(),
            guild_id="123456789",
            role_id="777888999",
            name="Squad Beta",
            description="Second squad",
            switch_cost=100,
            max_members=5,
            member_count=5,
            is_active=True,
            is_full=True,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
    ]


@pytest.fixture
def mock_squads_service():
    """Create a mock squads service."""
    service = AsyncMock()
    service.join_squad.return_value = JoinSquadResult(
        success=True,
        squad=Mock(name="Test Squad"),
        cost=50,
        new_balance=950
    )
    return service


class TestSquadSelectView:
    """Test suite for SquadSelectView."""
    
    def test_view_initialization(self, mock_squads, mock_squads_service):
        """Test view initialization with proper parameters."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service,
            timeout=60
        )
        
        assert view.squads == mock_squads
        assert view.current_squad is None
        assert view.user_balance == 1000
        assert view.user_id == "987654321"
        assert view.guild_id == "123456789"
        assert view.timeout == 60
        assert view.selected_squad_id is None
    
    def test_build_components(self, mock_squads, mock_squads_service):
        """Test building of select menu components."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        components = view.build()
        
        # Should return list of action rows
        assert isinstance(components, list)
        assert len(components) == 1
        
        # Action row should contain select menu
        action_row = components[0]
        assert hasattr(action_row, '_components')
    
    def test_build_with_current_squad(self, mock_squads, mock_squads_service):
        """Test building components when user has current squad."""
        current_squad = mock_squads[0]
        
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=current_squad,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        components = view.build()
        assert len(components) == 1
    
    def test_build_with_insufficient_balance(self, mock_squads, mock_squads_service):
        """Test building components when user has insufficient balance."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=10,  # Insufficient for any squad
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        components = view.build()
        assert len(components) == 1
        
        # Options should indicate insufficient balance
        # This would be tested more thoroughly in integration tests
    
    def test_start_sets_response(self, mock_squads, mock_squads_service):
        """Test that start method properly sets response."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        mock_response = Mock()
        view.start(mock_response)
        
        assert view._response == mock_response
        assert view._timeout_task is not None
    
    @pytest.mark.asyncio
    async def test_handle_interaction_success(self, mock_squads, mock_squads_service):
        """Test successful interaction handling."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        # Mock interaction event
        mock_interaction = Mock()
        mock_interaction.custom_id = "squad_select"
        mock_interaction.values = [str(mock_squads[0].id)]
        mock_interaction.create_initial_response = AsyncMock()
        
        mock_event = Mock()
        mock_event.interaction = mock_interaction
        
        await view.handle_interaction(mock_event)
        
        # Verify service was called
        mock_squads_service.join_squad.assert_called_once_with(
            "123456789",
            "987654321",
            mock_squads[0].id,
            1000
        )
        
        # Verify response was created
        mock_interaction.create_initial_response.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_handle_interaction_wrong_custom_id(self, mock_squads, mock_squads_service):
        """Test interaction handling with wrong custom ID."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        # Mock interaction with wrong custom ID
        mock_interaction = Mock()
        mock_interaction.custom_id = "wrong_id"
        
        mock_event = Mock()
        mock_event.interaction = mock_interaction
        
        await view.handle_interaction(mock_event)
        
        # Service should not be called
        mock_squads_service.join_squad.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_handle_interaction_already_processing(self, mock_squads, mock_squads_service):
        """Test interaction handling when already processing."""
        view = SquadSelectView(
            squads=mock_squads,
            current_squad=None,
            user_balance=1000,
            user_id="987654321",
            guild_id="123456789",
            squads_service=mock_squads_service
        )
        
        # Set processing flag
        view._is_processing = True
        
        # Mock interaction
        mock_interaction = Mock()
        mock_interaction.custom_id = "squad_select"
        mock_interaction.create_initial_response = AsyncMock()
        
        mock_event = Mock()
        mock_event.interaction = mock_interaction
        
        await view.handle_interaction(mock_event)
        
        # Service should not be called
        mock_squads_service.join_squad.assert_not_called()
        
        # Should respond with processing message
        mock_interaction.create_initial_response.assert_called_once()


class TestSquadConfirmView:
    """Test suite for SquadConfirmView."""
    
    def test_view_initialization(self):
        """Test confirmation view initialization."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?",
            confirm_label="Yes",
            cancel_label="No",
            timeout=30
        )
        
        assert view.title == "Confirm Action"
        assert view.description == "Are you sure?"
        assert view.confirm_label == "Yes"
        assert view.cancel_label == "No"
        assert view.timeout == 30
        assert view.confirmed is None
    
    def test_build_components(self):
        """Test building of confirmation buttons."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?"
        )
        
        components = view.build()
        
        # Should return list of action rows
        assert isinstance(components, list)
        assert len(components) == 1
        
        # Action row should contain buttons
        action_row = components[0]
        assert hasattr(action_row, '_components')
    
    def test_start_sets_response(self):
        """Test that start method properly sets response."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?"
        )
        
        mock_response = Mock()
        view.start(mock_response)
        
        assert view._response == mock_response
        assert view._timeout_task is not None
    
    @pytest.mark.asyncio
    async def test_handle_confirm_interaction(self):
        """Test handling of confirm button interaction."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?"
        )
        
        # Mock confirm interaction
        mock_interaction = Mock()
        mock_interaction.custom_id = "squad_confirm"
        mock_interaction.create_initial_response = AsyncMock()
        
        mock_event = Mock()
        mock_event.interaction = mock_interaction
        
        await view.handle_interaction(mock_event)
        
        # Should set confirmed to True
        assert view.confirmed is True
        
        # Should respond with success
        mock_interaction.create_initial_response.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_handle_cancel_interaction(self):
        """Test handling of cancel button interaction."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?"
        )
        
        # Mock cancel interaction
        mock_interaction = Mock()
        mock_interaction.custom_id = "squad_cancel"
        mock_interaction.create_initial_response = AsyncMock()
        
        mock_event = Mock()
        mock_event.interaction = mock_interaction
        
        await view.handle_interaction(mock_event)
        
        # Should set confirmed to False
        assert view.confirmed is False
        
        # Should respond with cancellation
        mock_interaction.create_initial_response.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_wait_for_confirmation(self):
        """Test waiting for user confirmation."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?",
            timeout=1  # Short timeout for testing
        )
        
        # Set confirmed immediately
        view.confirmed = True
        
        # Mock timeout task
        view._timeout_task = AsyncMock()
        
        result = await view.wait()
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_wait_timeout(self):
        """Test waiting with timeout."""
        view = SquadConfirmView(
            title="Confirm Action",
            description="Are you sure?",
            timeout=0.1  # Very short timeout
        )
        
        # Start with mock response
        mock_response = Mock()
        view.start(mock_response)
        
        result = await view.wait()
        
        # Should return False on timeout
        assert result is False