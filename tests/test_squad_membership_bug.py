"""Test squad membership creation bug fix using TDD."""

import pytest
from unittest.mock import AsyncMock, Mock

from web.models.squads import Squad, SquadMembership


class TestSquadMembershipCreationBug:
    """Test that SquadMembership creation includes guild_id parameter."""

    @pytest.mark.asyncio
    async def test_join_squad_creates_membership_with_guild_id(self):
        """Test that joining a squad creates SquadMembership with guild_id."""
        # This test verifies the bug is fixed: SquadMembership creation
        # must include guild_id as it's part of the composite primary key
        
        # Mock database session
        mock_db = AsyncMock()
        
        # Mock existing squad
        mock_squad = Mock(spec=Squad)
        mock_squad.id = "squad_123"
        mock_squad.guild_id = "guild_456" 
        mock_squad.name = "Test Squad"
        mock_squad.is_active = True
        
        # Mock database query results
        mock_db.execute.return_value.scalar_one_or_none.return_value = mock_squad
        
        # Mock the squad membership query to return None (user not in squad)
        mock_db.execute.return_value.scalar_one_or_none.side_effect = [
            mock_squad,  # First call: find squad
            None,        # Second call: check existing membership
        ]
        
        # Import the actual function to test
        from web.api.routes.squads import join_squad, JoinSquadRequest
        
        # Create request
        request = JoinSquadRequest(user_id="user_789")
        
        # Mock API key verification
        mock_api_key = Mock()
        
        # Track what gets added to the database
        added_objects = []
        
        def track_add(obj):
            added_objects.append(obj)
        
        mock_db.add = track_add
        mock_db.commit = AsyncMock()
        
        # Call the function
        result = await join_squad(
            guild_id="guild_456",
            squad_id="squad_123", 
            request=request,
            api_key=mock_api_key,
            db=mock_db
        )
        
        # Verify a SquadMembership was created and added
        assert len(added_objects) == 1, "Should have added exactly one object to database"
        
        membership = added_objects[0]
        assert isinstance(membership, SquadMembership), "Added object should be SquadMembership"
        
        # The bug fix: verify guild_id is included
        assert hasattr(membership, 'guild_id'), "SquadMembership should have guild_id attribute"
        assert membership.guild_id == "guild_456", "SquadMembership guild_id should match request guild_id"
        assert membership.user_id == "user_789", "SquadMembership user_id should match request user_id"
        assert membership.squad_id == "squad_123", "SquadMembership squad_id should match squad ID"
        
        # Verify response
        assert result["status"] == "joined"
        assert result["squad_id"] == "squad_123"

    @pytest.mark.asyncio
    async def test_squad_membership_composite_key_violation_handled(self):
        """Test that duplicate guild_id+user_id combinations are handled properly."""
        
        mock_db = AsyncMock()
        
        # Mock existing squad
        mock_squad = Mock(spec=Squad)
        mock_squad.id = "squad_123"
        mock_squad.guild_id = "guild_456"
        mock_squad.is_active = True
        
        # Mock existing membership (user already in a squad in this guild)
        mock_existing_membership = Mock(spec=SquadMembership)
        mock_existing_membership.guild_id = "guild_456"
        mock_existing_membership.user_id = "user_789"
        mock_existing_membership.squad_id = "different_squad_456"
        
        # Mock database query results
        mock_db.execute.return_value.scalar_one_or_none.side_effect = [
            mock_squad,                  # Find target squad
            mock_existing_membership,    # Find existing membership 
        ]
        
        from web.api.routes.squads import join_squad, JoinSquadRequest
        
        request = JoinSquadRequest(user_id="user_789")
        mock_api_key = Mock()
        
        # Track database operations
        deleted_objects = []
        added_objects = []
        
        def track_delete(delete_stmt):
            deleted_objects.append(delete_stmt)
            # Mock returning the deleted squad_id
            mock_result = Mock()
            mock_result.scalar_one_or_none.return_value = "different_squad_456"
            return mock_result
            
        def track_add(obj):
            added_objects.append(obj)
        
        mock_db.execute.side_effect = [
            # First two calls for the scalar_one_or_none queries above
            Mock(scalar_one_or_none=Mock(side_effect=[mock_squad, mock_existing_membership])),
            # Third call for the delete operation
            track_delete
        ]
        mock_db.add = track_add
        mock_db.commit = AsyncMock()
        
        # Call the function
        result = await join_squad(
            guild_id="guild_456",
            squad_id="squad_123",
            request=request, 
            api_key=mock_api_key,
            db=mock_db
        )
        
        # Should have removed old membership and added new one
        assert len(deleted_objects) > 0, "Should have deleted existing membership"
        assert len(added_objects) == 1, "Should have added new membership"
        
        new_membership = added_objects[0]
        assert new_membership.guild_id == "guild_456", "New membership should have correct guild_id"
        assert new_membership.user_id == "user_789", "New membership should have correct user_id"
        assert new_membership.squad_id == "squad_123", "New membership should have correct squad_id"

    @pytest.mark.asyncio 
    async def test_squad_membership_database_constraint_validation(self):
        """Test that SquadMembership model respects database constraints."""
        # This test ensures the SquadMembership model is defined correctly
        # with the composite primary key constraint
        
        # Test that SquadMembership has the expected table structure
        from web.models.squads import SquadMembership
        
        # Verify the table name
        assert SquadMembership.__tablename__ == "squad_memberships"
        
        # Verify the composite primary key columns
        primary_key_columns = []
        for column in SquadMembership.__table__.columns:
            if column.primary_key:
                primary_key_columns.append(column.name)
        
        # Should have composite primary key on guild_id and user_id
        assert "guild_id" in primary_key_columns, "guild_id should be part of primary key"
        assert "user_id" in primary_key_columns, "user_id should be part of primary key"
        assert len(primary_key_columns) == 2, "Should have exactly 2 primary key columns"
        
        # Verify foreign key constraint exists
        foreign_keys = SquadMembership.__table__.foreign_keys
        squad_fk_exists = any(
            fk.column.table.name == "squads" and fk.column.name == "id"
            for fk in foreign_keys
        )
        assert squad_fk_exists, "Should have foreign key constraint to squads.id"

    @pytest.mark.asyncio
    async def test_leave_squad_removes_membership_correctly(self):
        """Test that leaving a squad properly identifies membership by composite key."""
        
        mock_db = AsyncMock()
        
        # Mock the delete operation returning the squad_id of deleted membership
        mock_delete_result = Mock()
        mock_delete_result.scalar_one_or_none.return_value = "squad_123"
        mock_db.execute.return_value = mock_delete_result
        mock_db.commit = AsyncMock()
        
        from web.api.routes.squads import leave_squad, LeaveSquadRequest
        
        request = LeaveSquadRequest(user_id="user_789")
        mock_api_key = Mock()
        
        result = await leave_squad(
            guild_id="guild_456",
            request=request,
            api_key=mock_api_key, 
            db=mock_db
        )
        
        # Verify the delete statement used both guild_id and user_id
        mock_db.execute.assert_called_once()
        delete_call = mock_db.execute.call_args[0][0]
        
        # The delete statement should filter by both guild_id and user_id
        # This ensures we're using the composite primary key correctly
        delete_sql = str(delete_call)
        assert "guild_id" in delete_sql, "Delete should filter by guild_id"
        assert "user_id" in delete_sql, "Delete should filter by user_id"
        
        # Verify response
        assert result["status"] == "left"
        assert result["previous_squad_id"] == "squad_123"