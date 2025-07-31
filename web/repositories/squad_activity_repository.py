"""
Squad Activity Repository - Following SOLID principles.

This repository implements the Single Responsibility Principle by handling
only data access operations for squad activities. No business logic included.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, and_
from sqlalchemy.exc import IntegrityError

from web.models.squads import SquadActivity
import structlog

logger = structlog.get_logger()


class SquadActivityRepository:
    """
    Repository for squad activity data access operations.
    
    Following SRP: Only handles data persistence and retrieval.
    Following DIP: Depends on abstractions (AsyncSession interface).
    Following ISP: Implements minimal interface with focused methods.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize repository with database session dependency injection."""
        self.session = session
    
    async def create_activity(
        self,
        guild_id: str,
        user_id: str,
        activity_type: str,
        squad_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SquadActivity:
        """
        Create a new squad activity record.
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID  
            activity_type: Type of activity performed
            squad_id: Optional squad ID if activity is squad-specific
            metadata: Optional JSON metadata for activity details
            
        Returns:
            Created SquadActivity instance
            
        Raises:
            ValueError: If required validation fails
            IntegrityError: If database constraints are violated
        """
        # Basic validation (data layer responsibility)
        if not guild_id or len(guild_id) < 10:
            raise ValueError("Invalid guild_id format")
        
        if not user_id or len(user_id) < 10:
            raise ValueError("Invalid user_id format")
        
        if not activity_type or not activity_type.strip():
            raise ValueError("Activity type cannot be empty")
        
        # Convert squad_id to UUID if provided
        squad_uuid = None
        if squad_id:
            try:
                squad_uuid = UUID(squad_id)
            except ValueError:
                raise ValueError("Invalid squad_id format")
        
        try:
            activity = SquadActivity(
                guild_id=guild_id,
                user_id=user_id,
                activity_type=activity_type.strip(),
                squad_id=squad_uuid,
                metadata=metadata or {}
            )
            
            self.session.add(activity)
            await self.session.commit()
            await self.session.refresh(activity)
            
            logger.info(
                "Squad activity created",
                activity_id=str(activity.id),
                guild_id=guild_id,
                user_id=user_id,
                activity_type=activity_type
            )
            
            return activity
            
        except IntegrityError as e:
            await self.session.rollback()
            logger.error("Failed to create squad activity", error=str(e))
            raise
    
    async def get_activities_by_guild(
        self,
        guild_id: str,
        limit: int = 50,
        offset: int = 0,
        activity_type: Optional[str] = None
    ) -> List[SquadActivity]:
        """
        Get activities for a guild with pagination and optional filtering.
        
        Args:
            guild_id: Discord guild ID
            limit: Maximum number of activities to return
            offset: Number of activities to skip
            activity_type: Optional filter by activity type
            
        Returns:
            List of SquadActivity instances ordered by created_at DESC
        """
        query = select(SquadActivity).where(SquadActivity.guild_id == guild_id)
        
        if activity_type:
            query = query.where(SquadActivity.activity_type == activity_type)
        
        query = query.order_by(SquadActivity.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_activities_by_user(
        self,
        user_id: str,
        guild_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[SquadActivity]:
        """
        Get activities for a specific user in a guild.
        
        Args:
            user_id: Discord user ID
            guild_id: Discord guild ID
            limit: Maximum number of activities to return
            offset: Number of activities to skip
            
        Returns:
            List of SquadActivity instances ordered by created_at DESC
        """
        query = select(SquadActivity).where(
            and_(
                SquadActivity.user_id == user_id,
                SquadActivity.guild_id == guild_id
            )
        )
        
        query = query.order_by(SquadActivity.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_activities_by_squad(
        self,
        squad_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[SquadActivity]:
        """
        Get activities for a specific squad.
        
        Args:
            squad_id: Squad UUID string
            limit: Maximum number of activities to return
            offset: Number of activities to skip
            
        Returns:
            List of SquadActivity instances ordered by created_at DESC
        """
        try:
            squad_uuid = UUID(squad_id)
        except ValueError:
            raise ValueError("Invalid squad_id format")
        
        query = select(SquadActivity).where(SquadActivity.squad_id == squad_uuid)
        query = query.order_by(SquadActivity.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def get_activity_count_by_type(
        self,
        guild_id: str,
        activity_type: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> int:
        """
        Count activities by type within optional date range.
        
        Args:
            guild_id: Discord guild ID
            activity_type: Activity type to count
            date_from: Optional start date for filtering
            date_to: Optional end date for filtering
            
        Returns:
            Count of matching activities
        """
        query = select(func.count(SquadActivity.id)).where(
            and_(
                SquadActivity.guild_id == guild_id,
                SquadActivity.activity_type == activity_type
            )
        )
        
        if date_from:
            query = query.where(SquadActivity.created_at >= date_from)
        
        if date_to:
            query = query.where(SquadActivity.created_at <= date_to)
        
        result = await self.session.execute(query)
        return result.scalar() or 0
    
    async def get_recent_activities(
        self,
        guild_id: str,
        hours: int = 24,
        limit: int = 100
    ) -> List[SquadActivity]:
        """
        Get recent activities within specified hours.
        
        Args:
            guild_id: Discord guild ID
            hours: Number of hours to look back
            limit: Maximum number of activities to return
            
        Returns:
            List of recent SquadActivity instances ordered by created_at DESC
        """
        cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
        
        query = select(SquadActivity).where(
            and_(
                SquadActivity.guild_id == guild_id,
                SquadActivity.created_at >= cutoff_time
            )
        )
        
        query = query.order_by(SquadActivity.created_at.desc())
        query = query.limit(limit)
        
        result = await self.session.execute(query)
        return result.scalars().all()
    
    async def bulk_create_activities(
        self,
        activities_data: List[Dict[str, Any]]
    ) -> List[SquadActivity]:
        """
        Create multiple activities efficiently.
        
        Args:
            activities_data: List of activity data dictionaries
            
        Returns:
            List of created SquadActivity instances
            
        Raises:
            ValueError: If any activity data is invalid
            IntegrityError: If database constraints are violated
        """
        activities = []
        
        try:
            for data in activities_data:
                # Validate required fields
                guild_id = data.get("guild_id")
                user_id = data.get("user_id")
                activity_type = data.get("activity_type")
                
                if not guild_id or len(guild_id) < 10:
                    raise ValueError(f"Invalid guild_id format: {guild_id}")
                
                if not user_id or len(user_id) < 10:
                    raise ValueError(f"Invalid user_id format: {user_id}")
                
                if not activity_type or not activity_type.strip():
                    raise ValueError("Activity type cannot be empty")
                
                # Convert squad_id if provided
                squad_uuid = None
                squad_id = data.get("squad_id")
                if squad_id:
                    try:
                        squad_uuid = UUID(squad_id)
                    except ValueError:
                        raise ValueError(f"Invalid squad_id format: {squad_id}")
                
                activity = SquadActivity(
                    guild_id=guild_id,
                    user_id=user_id,
                    activity_type=activity_type.strip(),
                    squad_id=squad_uuid,
                    metadata=data.get("metadata", {})
                )
                
                activities.append(activity)
            
            # Bulk add all activities
            self.session.add_all(activities)
            await self.session.commit()
            
            # Refresh all to get IDs and timestamps
            for activity in activities:
                await self.session.refresh(activity)
            
            logger.info(
                "Bulk activities created",
                count=len(activities),
                activity_types=[a.activity_type for a in activities]
            )
            
            return activities
            
        except (ValueError, IntegrityError) as e:
            await self.session.rollback()
            logger.error("Failed to bulk create activities", error=str(e))
            raise
    
    async def get_activity_statistics(
        self,
        guild_id: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get activity statistics for a guild within date range.
        
        Args:
            guild_id: Discord guild ID
            date_from: Optional start date
            date_to: Optional end date
            
        Returns:
            Dictionary with activity statistics
        """
        base_query = select(SquadActivity).where(SquadActivity.guild_id == guild_id)
        
        if date_from:
            base_query = base_query.where(SquadActivity.created_at >= date_from)
        
        if date_to:
            base_query = base_query.where(SquadActivity.created_at <= date_to)
        
        # Total activity count
        total_query = select(func.count(SquadActivity.id)).select_from(base_query.subquery())
        total_result = await self.session.execute(total_query)
        total_count = total_result.scalar() or 0
        
        # Activity by type
        type_query = select(
            SquadActivity.activity_type,
            func.count(SquadActivity.id)
        ).where(SquadActivity.guild_id == guild_id)
        
        if date_from:
            type_query = type_query.where(SquadActivity.created_at >= date_from)
        
        if date_to:
            type_query = type_query.where(SquadActivity.created_at <= date_to)
        
        type_query = type_query.group_by(SquadActivity.activity_type)
        type_result = await self.session.execute(type_query)
        type_counts = dict(type_result.fetchall())
        
        # Unique user count
        user_query = select(func.count(func.distinct(SquadActivity.user_id))).where(
            SquadActivity.guild_id == guild_id
        )
        
        if date_from:
            user_query = user_query.where(SquadActivity.created_at >= date_from)
        
        if date_to:
            user_query = user_query.where(SquadActivity.created_at <= date_to)
        
        user_result = await self.session.execute(user_query)
        unique_users = user_result.scalar() or 0
        
        return {
            "total_activities": total_count,
            "activities_by_type": type_counts,
            "unique_users": unique_users,
            "date_range": {
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None
            }
        }
    
    async def cleanup_old_activities(
        self,
        guild_id: str,
        days_to_keep: int = 90
    ) -> int:
        """
        Clean up old activities beyond retention period.
        
        Args:
            guild_id: Discord guild ID
            days_to_keep: Number of days of activities to retain
            
        Returns:
            Number of activities deleted
        """
        cutoff_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_to_keep)
        
        # Count activities to be deleted
        count_query = select(func.count(SquadActivity.id)).where(
            and_(
                SquadActivity.guild_id == guild_id,
                SquadActivity.created_at < cutoff_date
            )
        )
        count_result = await self.session.execute(count_query)
        count_to_delete = count_result.scalar() or 0
        
        if count_to_delete > 0:
            # Delete old activities
            delete_query = select(SquadActivity).where(
                and_(
                    SquadActivity.guild_id == guild_id,
                    SquadActivity.created_at < cutoff_date
                )
            )
            
            result = await self.session.execute(delete_query)
            old_activities = result.scalars().all()
            
            for activity in old_activities:
                await self.session.delete(activity)
            
            await self.session.commit()
            
            logger.info(
                "Old activities cleaned up",
                guild_id=guild_id,
                deleted_count=count_to_delete,
                cutoff_date=cutoff_date.isoformat()
            )
        
        return count_to_delete