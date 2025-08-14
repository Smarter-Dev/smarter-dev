"""
Squad Integration Service - Following SOLID principles.

This service integrates the challenge system with the existing Discord squad system,
providing squad-based challenge participation and scoring capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, List, Optional, Protocol
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class SquadChallengeRole(Enum):
    """Roles for squad challenge participation."""
    CAPTAIN = "captain"  # Can submit for squad and manage members
    MEMBER = "member"    # Can submit for squad
    VIEWER = "viewer"    # Can view but not submit


@dataclass
class SquadChallengeResult:
    """
    Result of squad challenge operations.
    
    Contains operation status and relevant squad challenge data.
    """
    success: bool
    message: str
    squad_id: Optional[UUID] = None
    guild_id: Optional[str] = None
    participant_count: int = 0
    data: Optional[Dict[str, Any]] = None


@dataclass 
class SquadMemberInfo:
    """
    Information about a squad member for challenge purposes.
    
    Bridges Discord user data with squad membership.
    """
    user_id: str
    squad_id: UUID
    guild_id: str
    role: SquadChallengeRole
    joined_at: datetime
    is_active: bool = True


# Protocol definitions for existing system integration
class DiscordSquadProtocol(Protocol):
    """Protocol for existing Discord Squad model."""
    id: UUID
    guild_id: str
    role_id: str
    name: str
    description: Optional[str]
    switch_cost: int
    max_members: Optional[int]
    is_active: bool
    is_default: bool


class SquadMembershipProtocol(Protocol):
    """Protocol for existing SquadMembership model."""
    squad_id: UUID
    user_id: str
    guild_id: str
    joined_at: datetime


class SquadRepositoryProtocol(Protocol):
    """Protocol for accessing existing squad data."""
    
    async def get_squad_by_id(self, squad_id: UUID) -> Optional[DiscordSquadProtocol]:
        """Get squad by ID."""
        pass
    
    async def get_squads_by_guild(self, guild_id: str) -> List[DiscordSquadProtocol]:
        """Get all squads in a guild."""
        pass
    
    async def get_squad_members(self, squad_id: UUID) -> List[SquadMembershipProtocol]:
        """Get all members of a squad."""
        pass
    
    async def get_user_squad(self, user_id: str, guild_id: str) -> Optional[SquadMembershipProtocol]:
        """Get user's current squad in guild."""
        pass
    
    async def is_user_in_squad(self, user_id: str, squad_id: UUID) -> bool:
        """Check if user is in specific squad."""
        pass


class ChallengeProtocol(Protocol):
    """Protocol for challenge objects."""
    id: UUID
    campaign_id: UUID
    title: str


class CampaignProtocol(Protocol):
    """Protocol for campaign objects."""
    id: UUID
    guild_id: Optional[str]
    participant_type: str  # 'player' or 'squad'


class SquadIntegrationService:
    """
    Service for integrating challenges with the existing Discord squad system.
    
    Following SRP: Only handles squad-challenge integration logic.
    Following DIP: Depends on abstractions (repository protocols).
    Following OCP: Extensible for different squad integration strategies.
    """
    
    def __init__(self, squad_repository: SquadRepositoryProtocol):
        """
        Initialize service with squad repository dependency.
        
        Args:
            squad_repository: Repository for accessing existing squad data
        """
        self.squad_repository = squad_repository
    
    async def get_squad_for_challenge(
        self,
        user_id: str,
        campaign: CampaignProtocol
    ) -> SquadChallengeResult:
        """
        Get the squad a user should participate with for a campaign.
        
        Args:
            user_id: Discord user ID
            campaign: Campaign to participate in
            
        Returns:
            SquadChallengeResult with squad information
        """
        try:
            # Only applicable for squad-based campaigns
            if campaign.participant_type != "squad":
                return SquadChallengeResult(
                    success=False,
                    message="Campaign is not squad-based"
                )
            
            # Campaign must be guild-specific for squad participation
            if not campaign.guild_id:
                return SquadChallengeResult(
                    success=False,
                    message="Campaign is not guild-specific"
                )
            
            # Get user's current squad in the guild
            squad_membership = await self.squad_repository.get_user_squad(
                user_id=user_id,
                guild_id=campaign.guild_id
            )
            
            if not squad_membership:
                return SquadChallengeResult(
                    success=False,
                    message="User is not in any squad in this guild"
                )
            
            # Get squad details
            squad = await self.squad_repository.get_squad_by_id(squad_membership.squad_id)
            if not squad or not squad.is_active:
                return SquadChallengeResult(
                    success=False,
                    message="User's squad is not active"
                )
            
            # Get squad member count
            squad_members = await self.squad_repository.get_squad_members(squad.id)
            active_member_count = len([m for m in squad_members if m])
            
            logger.info(f"User {user_id} participating with squad {squad.id} ({squad.name})")
            
            return SquadChallengeResult(
                success=True,
                message=f"Participating with squad '{squad.name}'",
                squad_id=squad.id,
                guild_id=squad.guild_id,
                participant_count=active_member_count,
                data={
                    "squad_name": squad.name,
                    "squad_description": squad.description,
                    "member_count": active_member_count,
                    "max_members": squad.max_members
                }
            )
            
        except Exception as e:
            logger.exception(f"Error getting squad for user {user_id} in campaign {campaign.id}")
            return SquadChallengeResult(
                success=False,
                message=f"Failed to determine squad: {str(e)}"
            )
    
    async def can_user_submit_for_squad(
        self,
        user_id: str,
        squad_id: UUID
    ) -> bool:
        """
        Check if a user can submit challenges for a squad.
        
        Args:
            user_id: Discord user ID
            squad_id: Squad UUID
            
        Returns:
            True if user can submit for the squad
        """
        try:
            # Check if user is in the squad
            is_member = await self.squad_repository.is_user_in_squad(user_id, squad_id)
            
            if not is_member:
                return False
            
            # Get squad to ensure it's active
            squad = await self.squad_repository.get_squad_by_id(squad_id)
            if not squad or not squad.is_active:
                return False
            
            return True
            
        except Exception as e:
            logger.exception(f"Error checking submission permissions for user {user_id}, squad {squad_id}")
            return False
    
    async def get_squad_members_for_challenge(
        self,
        squad_id: UUID
    ) -> List[SquadMemberInfo]:
        """
        Get squad members with challenge participation info.
        
        Args:
            squad_id: Squad UUID
            
        Returns:
            List of squad members with challenge roles
        """
        try:
            squad = await self.squad_repository.get_squad_by_id(squad_id)
            if not squad:
                return []
            
            members = await self.squad_repository.get_squad_members(squad_id)
            
            squad_member_infos = []
            for member in members:
                # For now, all squad members are considered regular members
                # Future enhancement: Add squad role hierarchy
                member_info = SquadMemberInfo(
                    user_id=member.user_id,
                    squad_id=member.squad_id,
                    guild_id=member.guild_id,
                    role=SquadChallengeRole.MEMBER,
                    joined_at=member.joined_at,
                    is_active=True
                )
                squad_member_infos.append(member_info)
            
            return squad_member_infos
            
        except Exception as e:
            logger.exception(f"Error getting squad members for {squad_id}")
            return []
    
    async def get_squad_challenge_statistics(
        self,
        squad_id: UUID,
        campaign_id: Optional[UUID] = None
    ) -> Dict[str, Any]:
        """
        Get squad challenge participation statistics.
        
        Args:
            squad_id: Squad UUID
            campaign_id: Optional campaign to filter by
            
        Returns:
            Dictionary with squad challenge statistics
        """
        try:
            squad = await self.squad_repository.get_squad_by_id(squad_id)
            if not squad:
                return {}
            
            members = await self.squad_repository.get_squad_members(squad_id)
            
            # Basic statistics
            stats = {
                "squad_id": str(squad_id),
                "squad_name": squad.name,
                "total_members": len(members),
                "active_members": len([m for m in members if m]),
                "guild_id": squad.guild_id,
                "is_active": squad.is_active
            }
            
            # Future enhancement: Add challenge-specific statistics
            # like submission counts, success rates, etc.
            if campaign_id:
                stats["campaign_id"] = str(campaign_id)
                # Add campaign-specific statistics here
            
            return stats
            
        except Exception as e:
            logger.exception(f"Error getting squad statistics for {squad_id}")
            return {}
    
    async def validate_squad_challenge_access(
        self,
        user_id: str,
        challenge: ChallengeProtocol,
        campaign: CampaignProtocol
    ) -> SquadChallengeResult:
        """
        Validate if a user can participate in a challenge through their squad.
        
        Args:
            user_id: Discord user ID
            challenge: Challenge to participate in
            campaign: Campaign the challenge belongs to
            
        Returns:
            SquadChallengeResult with validation status
        """
        try:
            # Check if this is a squad-based campaign
            if campaign.participant_type != "squad":
                return SquadChallengeResult(
                    success=True,
                    message="Individual participation allowed"
                )
            
            # Get user's squad for this campaign
            squad_result = await self.get_squad_for_challenge(user_id, campaign)
            
            if not squad_result.success:
                return squad_result
            
            # Check if user can submit for their squad
            can_submit = await self.can_user_submit_for_squad(user_id, squad_result.squad_id)
            
            if not can_submit:
                return SquadChallengeResult(
                    success=False,
                    message="User cannot submit challenges for this squad"
                )
            
            logger.info(
                f"User {user_id} validated for challenge {challenge.id} "
                f"with squad {squad_result.squad_id}"
            )
            
            return SquadChallengeResult(
                success=True,
                message="Squad challenge access validated",
                squad_id=squad_result.squad_id,
                guild_id=squad_result.guild_id,
                data=squad_result.data
            )
            
        except Exception as e:
            logger.exception(f"Error validating squad access for user {user_id}, challenge {challenge.id}")
            return SquadChallengeResult(
                success=False,
                message=f"Validation error: {str(e)}"
            )
    
    async def get_participant_identifier(
        self,
        user_id: str,
        campaign: CampaignProtocol
    ) -> tuple[str, str]:
        """
        Get the participant identifier and type for a user in a campaign.
        
        Args:
            user_id: Discord user ID
            campaign: Campaign to participate in
            
        Returns:
            Tuple of (participant_id, participant_type)
        """
        try:
            if campaign.participant_type == "player":
                return user_id, "player"
            
            elif campaign.participant_type == "squad":
                squad_result = await self.get_squad_for_challenge(user_id, campaign)
                
                if not squad_result.success:
                    raise ValueError(f"Cannot get squad for user: {squad_result.message}")
                
                return str(squad_result.squad_id), "squad"
            
            else:
                raise ValueError(f"Unknown participant type: {campaign.participant_type}")
                
        except Exception as e:
            logger.exception(f"Error getting participant identifier for user {user_id}")
            raise ValueError(f"Failed to get participant identifier: {str(e)}")
    
    async def get_guild_squads_for_campaign(
        self,
        guild_id: str,
        include_inactive: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get all squads in a guild that can participate in campaigns.
        
        Args:
            guild_id: Discord guild ID
            include_inactive: Whether to include inactive squads
            
        Returns:
            List of squad information dictionaries
        """
        try:
            squads = await self.squad_repository.get_squads_by_guild(guild_id)
            
            squad_infos = []
            for squad in squads:
                if not include_inactive and not squad.is_active:
                    continue
                
                # Get member count
                members = await self.squad_repository.get_squad_members(squad.id)
                member_count = len(members)
                
                squad_info = {
                    "id": str(squad.id),
                    "name": squad.name,
                    "description": squad.description,
                    "member_count": member_count,
                    "max_members": squad.max_members,
                    "is_active": squad.is_active,
                    "is_default": squad.is_default,
                    "switch_cost": squad.switch_cost,
                    "role_id": squad.role_id
                }
                squad_infos.append(squad_info)
            
            return squad_infos
            
        except Exception as e:
            logger.exception(f"Error getting guild squads for {guild_id}")
            return []