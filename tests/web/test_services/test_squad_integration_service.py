"""Test cases for Squad Integration Service."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import AsyncMock, Mock

from web.services.squad_integration_service import (
    SquadIntegrationService,
    SquadChallengeResult,
    SquadChallengeRole,
    SquadMemberInfo
)


class TestSquadChallengeResult:
    """Test cases for SquadChallengeResult data structure."""
    
    def test_squad_challenge_result_success(self):
        """Test SquadChallengeResult creation for successful operation."""
        result = SquadChallengeResult(
            success=True,
            message="Squad found successfully",
            squad_id=uuid4(),
            guild_id="123456789",
            participant_count=5
        )
        
        assert result.success is True
        assert result.message == "Squad found successfully"
        assert result.squad_id is not None
        assert result.guild_id == "123456789"
        assert result.participant_count == 5
    
    def test_squad_challenge_result_failure(self):
        """Test SquadChallengeResult creation for failed operation."""
        result = SquadChallengeResult(
            success=False,
            message="User not in squad"
        )
        
        assert result.success is False
        assert result.message == "User not in squad"
        assert result.squad_id is None
        assert result.guild_id is None
        assert result.participant_count == 0


class TestSquadMemberInfo:
    """Test cases for SquadMemberInfo data structure."""
    
    def test_squad_member_info_creation(self):
        """Test SquadMemberInfo creation."""
        member_info = SquadMemberInfo(
            user_id="user123",
            squad_id=uuid4(),
            guild_id="guild123",
            role=SquadChallengeRole.MEMBER,
            joined_at=datetime.now(timezone.utc)
        )
        
        assert member_info.user_id == "user123"
        assert member_info.squad_id is not None
        assert member_info.guild_id == "guild123"
        assert member_info.role == SquadChallengeRole.MEMBER
        assert member_info.is_active is True


class TestSquadIntegrationService:
    """Test cases for Squad Integration Service functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_squad_repo = AsyncMock()
        self.service = SquadIntegrationService(
            squad_repository=self.mock_squad_repo
        )
        
        # Sample squad data
        self.sample_squad = Mock(
            id=uuid4(),
            guild_id="guild123",
            role_id="role456",
            description="A test squad",
            switch_cost=50,
            max_members=10,
            is_active=True,
            is_default=False
        )
        self.sample_squad.name = "Test Squad"
        
        # Sample squad membership
        self.sample_membership = Mock(
            squad_id=self.sample_squad.id,
            user_id="user123",
            guild_id="guild123",
            joined_at=datetime.now(timezone.utc)
        )
        
        # Sample campaign data
        self.squad_campaign = Mock(
            id=uuid4(),
            guild_id="guild123",
            participant_type="squad"
        )
        
        self.player_campaign = Mock(
            id=uuid4(),
            guild_id="guild123",
            participant_type="player"
        )
        
        # Sample challenge
        self.sample_challenge = Mock(
            id=uuid4(),
            campaign_id=self.squad_campaign.id,
            title="Test Challenge"
        )
    
    async def test_get_squad_for_challenge_success(self):
        """Test successful squad retrieval for challenge."""
        # Mock repository calls
        self.mock_squad_repo.get_user_squad.return_value = self.sample_membership
        self.mock_squad_repo.get_squad_by_id.return_value = self.sample_squad
        self.mock_squad_repo.get_squad_members.return_value = [self.sample_membership]
        
        # Act
        result = await self.service.get_squad_for_challenge(
            user_id="user123",
            campaign=self.squad_campaign
        )
        
        # Assert
        assert result.success is True
        assert result.squad_id == self.sample_squad.id
        assert result.guild_id == self.sample_squad.guild_id
        assert result.participant_count == 1
        assert "Participating with squad" in result.message
        assert result.data["squad_name"] == "Test Squad"
        
        # Verify repository calls
        self.mock_squad_repo.get_user_squad.assert_called_once_with(
            user_id="user123",
            guild_id="guild123"
        )
    
    async def test_get_squad_for_challenge_not_squad_campaign(self):
        """Test squad retrieval for non-squad campaign."""
        # Act
        result = await self.service.get_squad_for_challenge(
            user_id="user123",
            campaign=self.player_campaign
        )
        
        # Assert
        assert result.success is False
        assert "not squad-based" in result.message
        
        # Verify no repository calls made
        self.mock_squad_repo.get_user_squad.assert_not_called()
    
    async def test_get_squad_for_challenge_no_guild(self):
        """Test squad retrieval for campaign without guild."""
        global_campaign = Mock(
            id=uuid4(),
            guild_id=None,
            participant_type="squad"
        )
        
        # Act
        result = await self.service.get_squad_for_challenge(
            user_id="user123",
            campaign=global_campaign
        )
        
        # Assert
        assert result.success is False
        assert "not guild-specific" in result.message
    
    async def test_get_squad_for_challenge_user_not_in_squad(self):
        """Test squad retrieval when user is not in any squad."""
        # Mock no squad membership
        self.mock_squad_repo.get_user_squad.return_value = None
        
        # Act
        result = await self.service.get_squad_for_challenge(
            user_id="user123",
            campaign=self.squad_campaign
        )
        
        # Assert
        assert result.success is False
        assert "not in any squad" in result.message
    
    async def test_get_squad_for_challenge_inactive_squad(self):
        """Test squad retrieval when user's squad is inactive."""
        # Mock inactive squad
        inactive_squad = Mock(
            id=uuid4(),
            guild_id="guild123",
            name="Inactive Squad",
            is_active=False
        )
        
        self.mock_squad_repo.get_user_squad.return_value = self.sample_membership
        self.mock_squad_repo.get_squad_by_id.return_value = inactive_squad
        
        # Act
        result = await self.service.get_squad_for_challenge(
            user_id="user123",
            campaign=self.squad_campaign
        )
        
        # Assert
        assert result.success is False
        assert "not active" in result.message
    
    async def test_can_user_submit_for_squad_success(self):
        """Test successful permission check for squad submission."""
        # Mock repository calls
        self.mock_squad_repo.is_user_in_squad.return_value = True
        self.mock_squad_repo.get_squad_by_id.return_value = self.sample_squad
        
        # Act
        result = await self.service.can_user_submit_for_squad(
            user_id="user123",
            squad_id=self.sample_squad.id
        )
        
        # Assert
        assert result is True
        
        # Verify repository calls
        self.mock_squad_repo.is_user_in_squad.assert_called_once_with("user123", self.sample_squad.id)
        self.mock_squad_repo.get_squad_by_id.assert_called_once_with(self.sample_squad.id)
    
    async def test_can_user_submit_for_squad_not_member(self):
        """Test permission check when user is not squad member."""
        # Mock user not in squad
        self.mock_squad_repo.is_user_in_squad.return_value = False
        
        # Act
        result = await self.service.can_user_submit_for_squad(
            user_id="user123",
            squad_id=self.sample_squad.id
        )
        
        # Assert
        assert result is False
    
    async def test_can_user_submit_for_squad_inactive_squad(self):
        """Test permission check for inactive squad."""
        # Mock inactive squad
        inactive_squad = Mock(
            id=uuid4(),
            is_active=False
        )
        
        self.mock_squad_repo.is_user_in_squad.return_value = True
        self.mock_squad_repo.get_squad_by_id.return_value = inactive_squad
        
        # Act
        result = await self.service.can_user_submit_for_squad(
            user_id="user123",
            squad_id=inactive_squad.id
        )
        
        # Assert
        assert result is False
    
    async def test_get_squad_members_for_challenge(self):
        """Test getting squad members with challenge info."""
        # Mock additional members
        member1 = Mock(
            user_id="user1",
            squad_id=self.sample_squad.id,
            guild_id="guild123",
            joined_at=datetime.now(timezone.utc)
        )
        member2 = Mock(
            user_id="user2",
            squad_id=self.sample_squad.id,
            guild_id="guild123",
            joined_at=datetime.now(timezone.utc)
        )
        
        self.mock_squad_repo.get_squad_by_id.return_value = self.sample_squad
        self.mock_squad_repo.get_squad_members.return_value = [member1, member2]
        
        # Act
        result = await self.service.get_squad_members_for_challenge(self.sample_squad.id)
        
        # Assert
        assert len(result) == 2
        assert all(isinstance(member, SquadMemberInfo) for member in result)
        assert result[0].user_id == "user1"
        assert result[1].user_id == "user2"
        assert all(member.role == SquadChallengeRole.MEMBER for member in result)
        assert all(member.is_active for member in result)
    
    async def test_get_squad_members_for_challenge_no_squad(self):
        """Test getting members when squad doesn't exist."""
        # Mock no squad found
        self.mock_squad_repo.get_squad_by_id.return_value = None
        
        # Act
        result = await self.service.get_squad_members_for_challenge(uuid4())
        
        # Assert
        assert result == []
    
    async def test_get_squad_challenge_statistics(self):
        """Test getting squad challenge statistics."""
        # Mock squad and members
        self.mock_squad_repo.get_squad_by_id.return_value = self.sample_squad
        self.mock_squad_repo.get_squad_members.return_value = [
            self.sample_membership,
            Mock(user_id="user2", squad_id=self.sample_squad.id)
        ]
        
        # Act
        result = await self.service.get_squad_challenge_statistics(self.sample_squad.id)
        
        # Assert
        assert result["squad_id"] == str(self.sample_squad.id)
        assert result["squad_name"] == "Test Squad"
        assert result["total_members"] == 2
        assert result["active_members"] == 2
        assert result["guild_id"] == "guild123"
        assert result["is_active"] is True
    
    async def test_validate_squad_challenge_access_success(self):
        """Test successful squad challenge access validation."""
        # Mock successful squad retrieval
        self.mock_squad_repo.get_user_squad.return_value = self.sample_membership
        self.mock_squad_repo.get_squad_by_id.return_value = self.sample_squad
        self.mock_squad_repo.get_squad_members.return_value = [self.sample_membership]
        self.mock_squad_repo.is_user_in_squad.return_value = True
        
        # Act
        result = await self.service.validate_squad_challenge_access(
            user_id="user123",
            challenge=self.sample_challenge,
            campaign=self.squad_campaign
        )
        
        # Assert
        assert result.success is True
        assert result.squad_id == self.sample_squad.id
        assert "access validated" in result.message
    
    async def test_validate_squad_challenge_access_player_campaign(self):
        """Test access validation for player-based campaign."""
        # Act
        result = await self.service.validate_squad_challenge_access(
            user_id="user123",
            challenge=self.sample_challenge,
            campaign=self.player_campaign
        )
        
        # Assert
        assert result.success is True
        assert "Individual participation allowed" in result.message
    
    async def test_get_participant_identifier_player_campaign(self):
        """Test getting participant identifier for player campaign."""
        # Act
        participant_id, participant_type = await self.service.get_participant_identifier(
            user_id="user123",
            campaign=self.player_campaign
        )
        
        # Assert
        assert participant_id == "user123"
        assert participant_type == "player"
    
    async def test_get_participant_identifier_squad_campaign(self):
        """Test getting participant identifier for squad campaign."""
        # Mock squad retrieval
        self.mock_squad_repo.get_user_squad.return_value = self.sample_membership
        self.mock_squad_repo.get_squad_by_id.return_value = self.sample_squad
        self.mock_squad_repo.get_squad_members.return_value = [self.sample_membership]
        
        # Act
        participant_id, participant_type = await self.service.get_participant_identifier(
            user_id="user123",
            campaign=self.squad_campaign
        )
        
        # Assert
        assert participant_id == str(self.sample_squad.id)
        assert participant_type == "squad"
    
    async def test_get_participant_identifier_no_squad(self):
        """Test getting participant identifier when user has no squad."""
        # Mock no squad membership
        self.mock_squad_repo.get_user_squad.return_value = None
        
        # Act & Assert
        with pytest.raises(ValueError, match="Cannot get squad for user"):
            await self.service.get_participant_identifier(
                user_id="user123",
                campaign=self.squad_campaign
            )
    
    async def test_get_guild_squads_for_campaign(self):
        """Test getting all squads in a guild for campaigns."""
        # Mock multiple squads
        squad1 = Mock(
            id=uuid4(),
            description="First squad",
            is_active=True,
            is_default=False,
            switch_cost=50,
            role_id="role1",
            max_members=10
        )
        squad1.name = "Squad 1"
        
        squad2 = Mock(
            id=uuid4(),
            description="Second squad",
            is_active=True,
            is_default=True,
            switch_cost=0,
            role_id="role2",
            max_members=None
        )
        squad2.name = "Squad 2"
        
        self.mock_squad_repo.get_squads_by_guild.return_value = [squad1, squad2]
        
        # Mock member counts
        async def mock_get_squad_members(squad_id):
            if squad_id == squad1.id:
                return [Mock(), Mock(), Mock()]  # 3 members
            elif squad_id == squad2.id:
                return [Mock(), Mock()]  # 2 members
            return []
        
        self.mock_squad_repo.get_squad_members.side_effect = mock_get_squad_members
        
        # Act
        result = await self.service.get_guild_squads_for_campaign("guild123")
        
        # Assert
        assert len(result) == 2
        
        # Check first squad
        assert result[0]["id"] == str(squad1.id)
        assert result[0]["name"] == "Squad 1"
        assert result[0]["member_count"] == 3
        assert result[0]["is_default"] is False
        
        # Check second squad
        assert result[1]["id"] == str(squad2.id)
        assert result[1]["name"] == "Squad 2"
        assert result[1]["member_count"] == 2
        assert result[1]["is_default"] is True
    
    async def test_get_guild_squads_for_campaign_exclude_inactive(self):
        """Test getting guild squads excluding inactive ones."""
        # Mock active and inactive squads
        active_squad = Mock(
            id=uuid4(),
            is_active=True
        )
        active_squad.name = "Active Squad"
        
        inactive_squad = Mock(
            id=uuid4(),
            is_active=False
        )
        inactive_squad.name = "Inactive Squad"
        
        self.mock_squad_repo.get_squads_by_guild.return_value = [active_squad, inactive_squad]
        self.mock_squad_repo.get_squad_members.return_value = []
        
        # Act
        result = await self.service.get_guild_squads_for_campaign("guild123", include_inactive=False)
        
        # Assert
        assert len(result) == 1
        assert result[0]["name"] == "Active Squad"
    
    async def test_get_guild_squads_for_campaign_include_inactive(self):
        """Test getting guild squads including inactive ones."""
        # Mock active and inactive squads
        active_squad = Mock(
            id=uuid4(),
            is_active=True
        )
        active_squad.name = "Active Squad"
        
        inactive_squad = Mock(
            id=uuid4(),
            is_active=False
        )
        inactive_squad.name = "Inactive Squad"
        
        self.mock_squad_repo.get_squads_by_guild.return_value = [active_squad, inactive_squad]
        self.mock_squad_repo.get_squad_members.return_value = []
        
        # Act
        result = await self.service.get_guild_squads_for_campaign("guild123", include_inactive=True)
        
        # Assert
        assert len(result) == 2
        squad_names = [s["name"] for s in result]
        assert "Active Squad" in squad_names
        assert "Inactive Squad" in squad_names


class TestSquadChallengeRole:
    """Test cases for SquadChallengeRole enum."""
    
    def test_squad_challenge_role_values(self):
        """Test SquadChallengeRole enum values."""
        assert SquadChallengeRole.CAPTAIN.value == "captain"
        assert SquadChallengeRole.MEMBER.value == "member"
        assert SquadChallengeRole.VIEWER.value == "viewer"
    
    def test_squad_challenge_role_comparison(self):
        """Test SquadChallengeRole comparison."""
        assert SquadChallengeRole.CAPTAIN != SquadChallengeRole.MEMBER
        assert SquadChallengeRole.MEMBER != SquadChallengeRole.VIEWER
        assert SquadChallengeRole.CAPTAIN.value == "captain"