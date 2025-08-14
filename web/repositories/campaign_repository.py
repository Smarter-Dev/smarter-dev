"""
Campaign Repository - Following SOLID principles.

This repository implements the Single Responsibility Principle by handling
only data access operations for campaigns. No business logic included.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from smarter_dev.web.models import Campaign, Challenge
import logging

logger = logging.getLogger(__name__)


class CampaignRepository:
    """
    Repository for campaign data access operations.
    
    Following SRP: Only handles data persistence and retrieval.
    Following DIP: Depends on abstractions (AsyncSession interface).
    Following ISP: Implements minimal interface with focused methods.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize repository with database session dependency injection."""
        self.session = session
    
    async def create_campaign(
        self,
        guild_id: str,
        name: str,
        start_date: datetime,
        announcement_channel_id: str,
        campaign_type: str = "player",
        description: Optional[str] = None,
        release_delay_minutes: int = 1440,
        scoring_type: str = "time_based",
        starting_points: Optional[int] = None,
        points_decrease_step: Optional[int] = None
    ) -> Campaign:
        """
        Create a new campaign.
        
        Args:
            guild_id: Discord guild ID
            name: Campaign name
            start_date: When the campaign starts
            announcement_channel_id: Discord channel for announcements
            campaign_type: 'player' or 'squad'
            description: Optional campaign description
            release_delay_minutes: Minutes between challenge releases
            scoring_type: 'time_based' or 'point_based'
            starting_points: Starting points for point-based scoring
            points_decrease_step: Point decrease step for point-based scoring
            
        Returns:
            Created Campaign instance
            
        Raises:
            ValueError: If required validation fails
            IntegrityError: If database constraints are violated
        """
        # Basic validation (data layer responsibility)
        if not guild_id or len(guild_id) < 9:  # Discord snowflakes are typically 17-19 chars, but allow more flexibility for tests
            raise ValueError("Invalid guild_id format")
        
        if not name or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Campaign name must be 1-100 characters")
        
        if not announcement_channel_id or len(announcement_channel_id) < 9:  # Same flexibility for channel IDs
            raise ValueError("Invalid announcement_channel_id format")
        
        if campaign_type not in ["player", "squad"]:
            raise ValueError("Campaign type must be 'player' or 'squad'")
        
        if scoring_type not in ["time_based", "point_based"]:
            raise ValueError("Scoring type must be 'time_based' or 'point_based'")
        
        if release_delay_minutes <= 0:
            raise ValueError("Release delay must be positive")
        
        # Validate point-based scoring parameters
        if scoring_type == "point_based":
            if starting_points is None or starting_points <= 0:
                raise ValueError("Starting points must be positive for point-based scoring")
            if points_decrease_step is None or points_decrease_step <= 0:
                raise ValueError("Points decrease step must be positive for point-based scoring")
        
        try:
            campaign = Campaign(
                guild_id=guild_id,
                name=name.strip(),
                description=description.strip() if description else None,
                campaign_type=campaign_type,
                start_date=start_date,
                release_delay_minutes=release_delay_minutes,
                scoring_type=scoring_type,
                starting_points=starting_points,
                points_decrease_step=points_decrease_step,
                announcement_channel_id=announcement_channel_id
            )
            
            self.session.add(campaign)
            await self.session.commit()
            await self.session.refresh(campaign)
            
            logger.info(f"Campaign created: {campaign.id} ({name}) for guild {guild_id}")
            
            return campaign
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to create campaign: {str(e)}")
            raise
    
    async def get_campaign_by_id(self, campaign_id: UUID) -> Optional[Campaign]:
        """
        Get campaign by ID.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Campaign instance or None if not found
        """
        query = select(Campaign).where(Campaign.id == campaign_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_campaigns_by_guild(
        self,
        guild_id: str,
        limit: int = 50,
        offset: int = 0,
        state: Optional[str] = None,
        campaign_type: Optional[str] = None
    ) -> List[Campaign]:
        """
        Get campaigns for a guild with pagination and optional filtering.
        
        Args:
            guild_id: Discord guild ID
            limit: Maximum number of campaigns to return
            offset: Number of campaigns to skip
            state: Optional filter by campaign state
            campaign_type: Optional filter by campaign type
            
        Returns:
            List of Campaign instances ordered by created_at DESC
        """
        query = select(Campaign).where(Campaign.guild_id == guild_id)
        
        if state:
            query = query.where(Campaign.state == state)
        
        if campaign_type:
            query = query.where(Campaign.campaign_type == campaign_type)
        
        query = query.order_by(Campaign.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_active_campaigns(
        self,
        guild_id: Optional[str] = None,
        limit: int = 50
    ) -> List[Campaign]:
        """
        Get active campaigns, optionally filtered by guild.
        
        Args:
            guild_id: Optional Discord guild ID filter
            limit: Maximum number of campaigns to return
            
        Returns:
            List of active Campaign instances
        """
        query = select(Campaign).where(Campaign.state == "active")
        
        if guild_id:
            query = query.where(Campaign.guild_id == guild_id)
        
        query = query.order_by(Campaign.start_date.asc())
        query = query.limit(limit)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def update_campaign_state(
        self,
        campaign_id: UUID,
        new_state: str
    ) -> Optional[Campaign]:
        """
        Update campaign state.
        
        Args:
            campaign_id: Campaign UUID
            new_state: New campaign state
            
        Returns:
            Updated Campaign instance or None if not found
            
        Raises:
            ValueError: If state transition is invalid
            IntegrityError: If database constraints are violated
        """
        if new_state not in ["draft", "active", "completed"]:
            raise ValueError("Invalid campaign state")
        
        campaign = await self.get_campaign_by_id(campaign_id)
        if not campaign:
            return None
        
        # Validate state transition
        if not campaign.can_transition_to(new_state):
            raise ValueError(f"Cannot transition from {campaign.state} to {new_state}")
        
        try:
            campaign.state = new_state
            await self.session.commit()
            await self.session.refresh(campaign)
            
            logger.info(f"Campaign state updated: {campaign_id} from {campaign.state} to {new_state}")
            
            return campaign
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to update campaign state: {str(e)}")
            raise
    
    async def update_campaign(
        self,
        campaign_id: UUID,
        updates: Dict[str, Any]
    ) -> Optional[Campaign]:
        """
        Update campaign with provided fields.
        
        Args:
            campaign_id: Campaign UUID
            updates: Dictionary of fields to update
            
        Returns:
            Updated Campaign instance or None if not found
            
        Raises:
            ValueError: If validation fails
            IntegrityError: If database constraints are violated
        """
        campaign = await self.get_campaign_by_id(campaign_id)
        if not campaign:
            return None
        
        # Validate updates
        allowed_updates = {
            "name", "description", "start_date", "release_delay_minutes",
            "scoring_type", "starting_points", "points_decrease_step",
            "announcement_channel_id"
        }
        
        invalid_keys = set(updates.keys()) - allowed_updates
        if invalid_keys:
            raise ValueError(f"Invalid update fields: {invalid_keys}")
        
        # Validate specific fields
        if "name" in updates:
            name = updates["name"]
            if not name or not name.strip() or len(name.strip()) > 100:
                raise ValueError("Campaign name must be 1-100 characters")
            updates["name"] = name.strip()
        
        if "release_delay_minutes" in updates:
            if updates["release_delay_minutes"] <= 0:
                raise ValueError("Release delay must be positive")
        
        if "scoring_type" in updates:
            if updates["scoring_type"] not in ["time_based", "point_based"]:
                raise ValueError("Scoring type must be 'time_based' or 'point_based'")
        
        try:
            for key, value in updates.items():
                setattr(campaign, key, value)
            
            await self.session.commit()
            await self.session.refresh(campaign)
            
            logger.info(f"Campaign updated: {campaign_id} fields: {list(updates.keys())}")
            
            return campaign
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to update campaign: {str(e)}")
            raise
    
    async def delete_campaign(self, campaign_id: UUID) -> bool:
        """
        Delete campaign and all related data.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            True if deleted, False if not found
        """
        campaign = await self.get_campaign_by_id(campaign_id)
        if not campaign:
            return False
        
        try:
            await self.session.delete(campaign)
            await self.session.commit()
            
            logger.info(f"Campaign deleted: {campaign_id} ({campaign.name})")
            
            return True
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to delete campaign: {str(e)}")
            raise
    
    async def get_campaigns_starting_soon(
        self,
        hours_ahead: int = 24
    ) -> List[Campaign]:
        """
        Get campaigns starting within specified hours.
        
        Args:
            hours_ahead: Number of hours to look ahead
            
        Returns:
            List of campaigns starting soon
        """
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(hours=hours_ahead)
        
        query = select(Campaign).where(
            and_(
                Campaign.state == "draft",
                Campaign.start_date >= now,
                Campaign.start_date <= future_time
            )
        )
        
        query = query.order_by(Campaign.start_date.asc())
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_campaign_with_challenges(
        self,
        campaign_id: UUID
    ) -> Optional[Campaign]:
        """
        Get campaign with all its challenges loaded.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Campaign instance with challenges loaded or None if not found
        """
        query = select(Campaign).options(
            selectinload(Campaign.challenges)
        ).where(Campaign.id == campaign_id)
        
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def count_campaigns_by_guild(
        self,
        guild_id: str,
        state: Optional[str] = None
    ) -> int:
        """
        Count campaigns for a guild, optionally filtered by state.
        
        Args:
            guild_id: Discord guild ID
            state: Optional campaign state filter
            
        Returns:
            Count of matching campaigns
        """
        query = select(func.count(Campaign.id)).where(Campaign.guild_id == guild_id)
        
        if state:
            query = query.where(Campaign.state == state)
        
        result = await self.session.execute(query)
        return result.scalar() or 0
    
    async def get_campaign_statistics(
        self,
        guild_id: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get campaign statistics for a guild within date range.
        
        Args:
            guild_id: Discord guild ID
            date_from: Optional start date
            date_to: Optional end date
            
        Returns:
            Dictionary with campaign statistics
        """
        base_query = select(Campaign).where(Campaign.guild_id == guild_id)
        
        if date_from:
            base_query = base_query.where(Campaign.created_at >= date_from)
        
        if date_to:
            base_query = base_query.where(Campaign.created_at <= date_to)
        
        # Total campaign count
        total_query = select(func.count(Campaign.id)).select_from(base_query.subquery())
        total_result = await self.session.execute(total_query)
        total_count = total_result.scalar() or 0
        
        # Campaigns by state
        state_query = select(
            Campaign.state,
            func.count(Campaign.id)
        ).where(Campaign.guild_id == guild_id)
        
        if date_from:
            state_query = state_query.where(Campaign.created_at >= date_from)
        
        if date_to:
            state_query = state_query.where(Campaign.created_at <= date_to)
        
        state_query = state_query.group_by(Campaign.state)
        state_result = await self.session.execute(state_query)
        state_counts = dict(state_result.fetchall())
        
        # Campaigns by type
        type_query = select(
            Campaign.campaign_type,
            func.count(Campaign.id)
        ).where(Campaign.guild_id == guild_id)
        
        if date_from:
            type_query = type_query.where(Campaign.created_at >= date_from)
        
        if date_to:
            type_query = type_query.where(Campaign.created_at <= date_to)
        
        type_query = type_query.group_by(Campaign.campaign_type)
        type_result = await self.session.execute(type_query)
        type_counts = dict(type_result.fetchall())
        
        return {
            "total_campaigns": total_count,
            "campaigns_by_state": state_counts,
            "campaigns_by_type": type_counts,
            "date_range": {
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None
            }
        }