"""
Challenge Repository - Following SOLID principles.

This repository implements the Single Responsibility Principle by handling
only data access operations for challenges. No business logic included.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, and_, or_, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from smarter_dev.web.models import Challenge, Campaign, GeneratedInputCache, ChallengeSubmission
import logging

logger = logging.getLogger(__name__)


class ChallengeRepository:
    """
    Repository for challenge data access operations.
    
    Following SRP: Only handles data persistence and retrieval.
    Following DIP: Depends on abstractions (AsyncSession interface).
    Following ISP: Implements minimal interface with focused methods.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize repository with database session dependency injection."""
        self.session = session
    
    async def create_challenge(
        self,
        campaign_id: UUID,
        order_position: int,
        title: str,
        description: str,
        generation_script: str,
        categories: Optional[List[str]] = None,
        difficulty_level: Optional[int] = None
    ) -> Challenge:
        """
        Create a new challenge.
        
        Args:
            campaign_id: Campaign UUID this challenge belongs to
            order_position: Position in the campaign sequence (1-based)
            title: Challenge title
            description: Challenge description in Markdown
            generation_script: Python script for generating inputs
            categories: Optional list of categories/tags
            difficulty_level: Optional difficulty level (1-10)
            
        Returns:
            Created Challenge instance
            
        Raises:
            ValueError: If required validation fails
            IntegrityError: If database constraints are violated
        """
        # Basic validation (data layer responsibility)
        if not isinstance(campaign_id, UUID):
            raise ValueError("Invalid campaign_id format")
        
        if order_position <= 0:
            raise ValueError("Order position must be positive")
        
        if not title or not title.strip() or len(title.strip()) > 200:
            raise ValueError("Challenge title must be 1-200 characters")
        
        if not description or not description.strip():
            raise ValueError("Challenge description cannot be empty")
        
        if not generation_script or not generation_script.strip():
            raise ValueError("Generation script cannot be empty")
        
        if difficulty_level is not None and (difficulty_level < 1 or difficulty_level > 10):
            raise ValueError("Difficulty level must be between 1 and 10")
        
        try:
            challenge = Challenge(
                campaign_id=campaign_id,
                order_position=order_position,
                title=title.strip(),
                description=description.strip(),
                generation_script=generation_script.strip(),
                categories=categories or [],
                difficulty_level=difficulty_level
            )
            
            self.session.add(challenge)
            await self.session.commit()
            await self.session.refresh(challenge)
            
            logger.info(f"Challenge created: {challenge.id} ({title}) for campaign {campaign_id}")
            
            return challenge
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to create challenge: {str(e)}")
            raise
    
    async def get_challenge_by_id(self, challenge_id: UUID) -> Optional[Challenge]:
        """
        Get challenge by ID.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            Challenge instance or None if not found
        """
        query = select(Challenge).where(Challenge.id == challenge_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_challenges_by_campaign(
        self,
        campaign_id: UUID,
        limit: Optional[int] = None,
        offset: int = 0,
        order_by_position: bool = True
    ) -> List[Challenge]:
        """
        Get challenges for a campaign.
        
        Args:
            campaign_id: Campaign UUID
            limit: Optional maximum number of challenges to return
            offset: Number of challenges to skip
            order_by_position: Whether to order by position (default) or creation date
            
        Returns:
            List of Challenge instances ordered by position or creation date
        """
        query = select(Challenge).where(Challenge.campaign_id == campaign_id)
        
        if order_by_position:
            query = query.order_by(Challenge.order_position.asc())
        else:
            query = query.order_by(Challenge.created_at.asc())
        
        if limit:
            query = query.limit(limit)
        if offset:
            query = query.offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_challenge_by_position(
        self,
        campaign_id: UUID,
        position: int
    ) -> Optional[Challenge]:
        """
        Get challenge by its position in a campaign.
        
        Args:
            campaign_id: Campaign UUID
            position: Challenge position (1-based)
            
        Returns:
            Challenge instance or None if not found
        """
        query = select(Challenge).where(
            and_(
                Challenge.campaign_id == campaign_id,
                Challenge.order_position == position
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_next_challenge(
        self,
        campaign_id: UUID,
        current_position: int
    ) -> Optional[Challenge]:
        """
        Get the next challenge after a given position.
        
        Args:
            campaign_id: Campaign UUID
            current_position: Current challenge position
            
        Returns:
            Next Challenge instance or None if no more challenges
        """
        query = select(Challenge).where(
            and_(
                Challenge.campaign_id == campaign_id,
                Challenge.order_position > current_position
            )
        ).order_by(Challenge.order_position.asc()).limit(1)
        
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def get_released_challenges(
        self,
        campaign_id: UUID,
        campaign_start_date: datetime,
        release_delay_minutes: int,
        current_time: Optional[datetime] = None
    ) -> List[Challenge]:
        """
        Get challenges that have been released based on timing.
        
        Args:
            campaign_id: Campaign UUID
            campaign_start_date: When the campaign started
            release_delay_minutes: Minutes between releases
            current_time: Current time (defaults to now)
            
        Returns:
            List of released Challenge instances ordered by position
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        
        # Get all challenges for the campaign
        all_challenges = await self.get_challenges_by_campaign(campaign_id)
        
        # Filter to only released challenges
        released_challenges = []
        for challenge in all_challenges:
            if challenge.is_released(campaign_start_date, release_delay_minutes, current_time):
                released_challenges.append(challenge)
        
        return released_challenges
    
    async def update_challenge(
        self,
        challenge_id: UUID,
        updates: Dict[str, Any]
    ) -> Optional[Challenge]:
        """
        Update challenge with provided fields.
        
        Args:
            challenge_id: Challenge UUID
            updates: Dictionary of fields to update
            
        Returns:
            Updated Challenge instance or None if not found
            
        Raises:
            ValueError: If validation fails
            IntegrityError: If database constraints are violated
        """
        challenge = await self.get_challenge_by_id(challenge_id)
        if not challenge:
            return None
        
        # Validate updates
        allowed_updates = {
            "title", "description", "generation_script", "categories", "difficulty_level"
        }
        
        invalid_keys = set(updates.keys()) - allowed_updates
        if invalid_keys:
            raise ValueError(f"Invalid update fields: {invalid_keys}")
        
        # Validate specific fields
        if "title" in updates:
            title = updates["title"]
            if not title or not title.strip() or len(title.strip()) > 200:
                raise ValueError("Challenge title must be 1-200 characters")
            updates["title"] = title.strip()
        
        if "description" in updates:
            description = updates["description"]
            if not description or not description.strip():
                raise ValueError("Challenge description cannot be empty")
            updates["description"] = description.strip()
        
        if "generation_script" in updates:
            script = updates["generation_script"]
            if not script or not script.strip():
                raise ValueError("Generation script cannot be empty")
            updates["generation_script"] = script.strip()
            # Update script timestamp when script changes
            challenge.update_script(updates["generation_script"])
            # Remove from updates since we handled it with update_script method
            del updates["generation_script"]
        
        if "difficulty_level" in updates:
            level = updates["difficulty_level"]
            if level is not None and (level < 1 or level > 10):
                raise ValueError("Difficulty level must be between 1 and 10")
        
        try:
            for key, value in updates.items():
                setattr(challenge, key, value)
            
            await self.session.commit()
            await self.session.refresh(challenge)
            
            logger.info(f"Challenge updated: {challenge_id} fields: {list(updates.keys())}")
            
            return challenge
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to update challenge: {str(e)}")
            raise
    
    async def update_generation_script(
        self,
        challenge_id: UUID,
        new_script: str
    ) -> Optional[Challenge]:
        """
        Update challenge generation script and invalidate cached inputs.
        
        Args:
            challenge_id: Challenge UUID
            new_script: New generation script
            
        Returns:
            Updated Challenge instance or None if not found
        """
        if not new_script or not new_script.strip():
            raise ValueError("Generation script cannot be empty")
        
        challenge = await self.get_challenge_by_id(challenge_id)
        if not challenge:
            return None
        
        try:
            # Update the script and timestamp
            challenge.update_script(new_script.strip())
            
            # Invalidate all cached inputs for this challenge
            await self._invalidate_cached_inputs(challenge_id)
            
            await self.session.commit()
            await self.session.refresh(challenge)
            
            logger.info(f"Challenge script updated and cache invalidated: {challenge_id}")
            
            return challenge
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to update challenge script: {str(e)}")
            raise
    
    async def _invalidate_cached_inputs(self, challenge_id: UUID) -> None:
        """
        Invalidate all cached inputs for a challenge.
        
        Args:
            challenge_id: Challenge UUID
        """
        # Update all cached inputs for this challenge to invalid
        from sqlalchemy import update
        
        stmt = update(GeneratedInputCache).where(
            GeneratedInputCache.challenge_id == challenge_id
        ).values(is_valid=False)
        
        await self.session.execute(stmt)
    
    async def delete_challenge(self, challenge_id: UUID) -> bool:
        """
        Delete challenge and all related data.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            True if deleted, False if not found
        """
        challenge = await self.get_challenge_by_id(challenge_id)
        if not challenge:
            return False
        
        try:
            await self.session.delete(challenge)
            await self.session.commit()
            
            logger.info(f"Challenge deleted: {challenge_id} ({challenge.title})")
            
            return True
            
        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to delete challenge: {str(e)}")
            raise
    
    async def reorder_challenges(
        self,
        campaign_id: UUID,
        challenge_orders: Dict[UUID, int]
    ) -> List[Challenge]:
        """
        Reorder challenges in a campaign.
        
        Args:
            campaign_id: Campaign UUID
            challenge_orders: Dict mapping challenge IDs to new positions
            
        Returns:
            List of updated Challenge instances
            
        Raises:
            ValueError: If validation fails
            IntegrityError: If database constraints are violated
        """
        # Validate positions are positive and unique
        positions = list(challenge_orders.values())
        if any(pos <= 0 for pos in positions):
            raise ValueError("All positions must be positive")
        
        if len(set(positions)) != len(positions):
            raise ValueError("Positions must be unique")
        
        # Get all challenges to update
        challenges_to_update = []
        for challenge_id in challenge_orders.keys():
            challenge = await self.get_challenge_by_id(challenge_id)
            if not challenge:
                raise ValueError(f"Challenge not found: {challenge_id}")
            if challenge.campaign_id != campaign_id:
                raise ValueError(f"Challenge {challenge_id} does not belong to campaign {campaign_id}")
            challenges_to_update.append(challenge)
        
        try:
            # Update positions
            for challenge in challenges_to_update:
                new_position = challenge_orders[challenge.id]
                challenge.order_position = new_position
            
            await self.session.commit()
            
            # Refresh and return all updated challenges
            for challenge in challenges_to_update:
                await self.session.refresh(challenge)
            
            logger.info(f"Challenges reordered for campaign {campaign_id}: {len(challenges_to_update)} challenges")
            
            return sorted(challenges_to_update, key=lambda c: c.order_position)
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error(f"Failed to reorder challenges: {str(e)}")
            raise
    
    async def count_challenges_by_campaign(self, campaign_id: UUID) -> int:
        """
        Count challenges in a campaign.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Count of challenges
        """
        query = select(func.count(Challenge.id)).where(Challenge.campaign_id == campaign_id)
        result = await self.session.execute(query)
        return result.scalar() or 0
    
    async def get_challenges_by_category(
        self,
        category: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Challenge]:
        """
        Get challenges that contain a specific category.
        
        Args:
            category: Category to search for
            limit: Maximum number of challenges to return
            offset: Number of challenges to skip
            
        Returns:
            List of Challenge instances containing the category
        """
        # Use JSON contains operator to search in categories array
        query = select(Challenge).where(
            Challenge.categories.contains([category])
        ).order_by(Challenge.created_at.desc()).limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_challenges_by_difficulty(
        self,
        difficulty_level: int,
        limit: int = 50,
        offset: int = 0
    ) -> List[Challenge]:
        """
        Get challenges by difficulty level.
        
        Args:
            difficulty_level: Difficulty level (1-10)
            limit: Maximum number of challenges to return
            offset: Number of challenges to skip
            
        Returns:
            List of Challenge instances with the specified difficulty
        """
        if difficulty_level < 1 or difficulty_level > 10:
            raise ValueError("Difficulty level must be between 1 and 10")
        
        query = select(Challenge).where(
            Challenge.difficulty_level == difficulty_level
        ).order_by(Challenge.created_at.desc()).limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_challenge_statistics(
        self,
        campaign_id: Optional[UUID] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get challenge statistics, optionally filtered by campaign and date range.
        
        Args:
            campaign_id: Optional campaign UUID filter
            date_from: Optional start date
            date_to: Optional end date
            
        Returns:
            Dictionary with challenge statistics
        """
        base_query = select(Challenge)
        
        if campaign_id:
            base_query = base_query.where(Challenge.campaign_id == campaign_id)
        
        if date_from:
            base_query = base_query.where(Challenge.created_at >= date_from)
        
        if date_to:
            base_query = base_query.where(Challenge.created_at <= date_to)
        
        # Total challenge count
        total_query = select(func.count(Challenge.id)).select_from(base_query.subquery())
        total_result = await self.session.execute(total_query)
        total_count = total_result.scalar() or 0
        
        # Average difficulty level (excluding None values)
        avg_difficulty_query = select(func.avg(Challenge.difficulty_level)).where(
            Challenge.difficulty_level.isnot(None)
        )
        
        if campaign_id:
            avg_difficulty_query = avg_difficulty_query.where(Challenge.campaign_id == campaign_id)
        if date_from:
            avg_difficulty_query = avg_difficulty_query.where(Challenge.created_at >= date_from)
        if date_to:
            avg_difficulty_query = avg_difficulty_query.where(Challenge.created_at <= date_to)
        
        avg_difficulty_result = await self.session.execute(avg_difficulty_query)
        avg_difficulty = avg_difficulty_result.scalar()
        
        # Challenges by difficulty level
        difficulty_query = select(
            Challenge.difficulty_level,
            func.count(Challenge.id)
        )
        
        if campaign_id:
            difficulty_query = difficulty_query.where(Challenge.campaign_id == campaign_id)
        if date_from:
            difficulty_query = difficulty_query.where(Challenge.created_at >= date_from)
        if date_to:
            difficulty_query = difficulty_query.where(Challenge.created_at <= date_to)
        
        difficulty_query = difficulty_query.group_by(Challenge.difficulty_level)
        difficulty_result = await self.session.execute(difficulty_query)
        difficulty_counts = dict(difficulty_result.fetchall())
        
        return {
            "total_challenges": total_count,
            "average_difficulty": float(avg_difficulty) if avg_difficulty else None,
            "challenges_by_difficulty": difficulty_counts,
            "date_range": {
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None
            }
        }