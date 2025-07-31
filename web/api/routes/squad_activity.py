"""
Squad Activity API Routes - Following SOLID principles.

API routes for activity tracking and health metrics.
Following OCP: Open for extension through activity types and metadata.
Following SRP: Each endpoint has single responsibility.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, validator
from uuid import UUID
import structlog

from web.database import get_db
from web.auth.api import verify_api_key
from web.repositories.squad_activity_repository import SquadActivityRepository
from web.services.squad_health_service import SquadHealthService

router = APIRouter()
logger = structlog.get_logger()


# Request/Response models following OCP - extensible through metadata
class ActivityCreateRequest(BaseModel):
    """Request model for creating activity - extensible via metadata."""
    
    guild_id: str = Field(..., min_length=10, description="Discord guild ID")
    user_id: str = Field(..., min_length=10, description="Discord user ID")
    activity_type: str = Field(..., min_length=1, max_length=100, description="Activity type")
    squad_id: Optional[str] = Field(None, description="Squad ID if activity is squad-specific")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Extensible metadata")
    
    @validator('guild_id', 'user_id')
    def validate_discord_id(cls, v):
        if len(v) < 10:
            raise ValueError('Discord IDs must be at least 10 characters')
        return v


class ActivityResponse(BaseModel):
    """Response model for activity."""
    
    id: str
    guild_id: str
    user_id: str
    squad_id: Optional[str]
    activity_type: str
    metadata: Dict[str, Any]
    created_at: datetime
    updated_at: datetime


class BulkActivityRequest(BaseModel):
    """Request model for bulk activity creation."""
    
    activities: List[Dict[str, Any]] = Field(..., min_items=1, max_items=100)


class HealthScoreResponse(BaseModel):
    """Response model for health score."""
    
    health_score: float = Field(..., ge=0.0, le=1.0)
    calculated_at: datetime
    analysis_period_days: int
    squad_id: str


class EngagementScoreResponse(BaseModel):
    """Response model for engagement score."""
    
    engagement_score: float = Field(..., ge=0.0, le=1.0)
    calculated_at: datetime
    analysis_period_days: int
    squad_id: str


class HealthReportResponse(BaseModel):
    """Response model for comprehensive health report."""
    
    health_score: float
    engagement_score: float
    activity_summary: Dict[str, Any]
    recommendations: List[str]
    trends: Optional[Dict[str, Any]]
    generated_at: datetime
    squad_id: str


class ActivityListResponse(BaseModel):
    """Response model for activity lists."""
    
    activities: List[ActivityResponse]
    total_count: int
    page_info: Dict[str, Any]


class ActivityStatisticsResponse(BaseModel):
    """Response model for activity statistics."""
    
    total_activities: int
    activities_by_type: Dict[str, int]
    unique_users: int
    date_range: Dict[str, Optional[str]]


# Dependency injection for services (DIP compliance)
async def get_activity_repository(session: AsyncSession = Depends(get_db)) -> SquadActivityRepository:
    """Get activity repository instance."""
    return SquadActivityRepository(session)


async def get_health_service(
    activity_repo: SquadActivityRepository = Depends(get_activity_repository)
) -> SquadHealthService:
    """Get health service instance."""
    return SquadHealthService(activity_repo)


# Activity CRUD endpoints
@router.post("/squads/activities", response_model=ActivityResponse, status_code=201)
async def create_activity(
    request: ActivityCreateRequest,
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> ActivityResponse:
    """
    Create a new squad activity.
    
    Following OCP: Supports any activity type and custom metadata.
    """
    try:
        activity = await activity_repo.create_activity(
            guild_id=request.guild_id,
            user_id=request.user_id,
            activity_type=request.activity_type,
            squad_id=request.squad_id,
            metadata=request.metadata
        )
        
        return ActivityResponse(
            id=str(activity.id),
            guild_id=activity.guild_id,
            user_id=activity.user_id,
            squad_id=str(activity.squad_id) if activity.squad_id else None,
            activity_type=activity.activity_type,
            metadata=activity.metadata,
            created_at=activity.created_at,
            updated_at=activity.updated_at
        )
        
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("Failed to create activity", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create activity")


@router.get("/guilds/{guild_id}/activities", response_model=ActivityListResponse)
async def get_guild_activities(
    guild_id: str,
    limit: int = Query(50, ge=1, le=200, description="Number of activities to return"),
    offset: int = Query(0, ge=0, description="Number of activities to skip"),
    activity_type: Optional[str] = Query(None, description="Filter by activity type"),
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> ActivityListResponse:
    """Get activities for a guild with pagination and filtering."""
    try:
        activities = await activity_repo.get_activities_by_guild(
            guild_id=guild_id,
            limit=limit,
            offset=offset,
            activity_type=activity_type
        )
        
        # Get total count for pagination
        total_count = await activity_repo.get_activity_count_by_type(
            guild_id=guild_id,
            activity_type=activity_type or ""
        ) if activity_type else len(activities)  # Simplified for now
        
        activity_responses = [
            ActivityResponse(
                id=str(activity.id),
                guild_id=activity.guild_id,
                user_id=activity.user_id,
                squad_id=str(activity.squad_id) if activity.squad_id else None,
                activity_type=activity.activity_type,
                metadata=activity.metadata,
                created_at=activity.created_at,
                updated_at=activity.updated_at
            )
            for activity in activities
        ]
        
        return ActivityListResponse(
            activities=activity_responses,
            total_count=total_count,
            page_info={
                "limit": limit,
                "offset": offset,
                "has_more": len(activities) == limit
            }
        )
        
    except Exception as e:
        logger.error("Failed to get guild activities", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get activities")


@router.get("/guilds/{guild_id}/users/{user_id}/activities", response_model=Dict[str, Any])
async def get_user_activities(
    guild_id: str,
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Get activities for a specific user."""
    try:
        activities = await activity_repo.get_activities_by_user(
            user_id=user_id,
            guild_id=guild_id,
            limit=limit,
            offset=offset
        )
        
        activity_responses = [
            ActivityResponse(
                id=str(activity.id),
                guild_id=activity.guild_id,
                user_id=activity.user_id,
                squad_id=str(activity.squad_id) if activity.squad_id else None,
                activity_type=activity.activity_type,
                metadata=activity.metadata,
                created_at=activity.created_at,
                updated_at=activity.updated_at
            )
            for activity in activities
        ]
        
        return {
            "user_id": user_id,
            "activities": activity_responses,
            "total_count": len(activities)
        }
        
    except Exception as e:
        logger.error("Failed to get user activities", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get user activities")


@router.get("/squads/{squad_id}/activities", response_model=Dict[str, Any])
async def get_squad_activities(
    squad_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    activity_type: Optional[str] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Get activities for a specific squad with filtering."""
    try:
        # Validate squad_id format
        try:
            UUID(squad_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid squad ID format")
        
        activities = await activity_repo.get_activities_by_squad(
            squad_id=squad_id,
            limit=limit,
            offset=offset
        )
        
        # Apply additional filters if specified
        if date_from:
            activities = [a for a in activities if a.created_at >= date_from]
        if date_to:
            activities = [a for a in activities if a.created_at <= date_to]
        if activity_type:
            activities = [a for a in activities if a.activity_type == activity_type]
        
        activity_responses = [
            ActivityResponse(
                id=str(activity.id),
                guild_id=activity.guild_id,
                user_id=activity.user_id,
                squad_id=str(activity.squad_id) if activity.squad_id else None,
                activity_type=activity.activity_type,
                metadata=activity.metadata,
                created_at=activity.created_at,
                updated_at=activity.updated_at
            )
            for activity in activities
        ]
        
        filters_applied = {}
        if activity_type:
            filters_applied["activity_type"] = activity_type
        if date_from:
            filters_applied["date_from"] = date_from.isoformat()
        if date_to:
            filters_applied["date_to"] = date_to.isoformat()
        
        return {
            "squad": {"id": squad_id},
            "activities": activity_responses,
            "total_count": len(activity_responses),
            "filters_applied": filters_applied
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get squad activities", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get squad activities")


@router.post("/squads/activities/bulk", status_code=201)
async def bulk_create_activities(
    request: BulkActivityRequest,
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Create multiple activities efficiently."""
    try:
        activities = await activity_repo.bulk_create_activities(request.activities)
        
        activity_responses = [
            ActivityResponse(
                id=str(activity.id),
                guild_id=activity.guild_id,
                user_id=activity.user_id,
                squad_id=str(activity.squad_id) if activity.squad_id else None,
                activity_type=activity.activity_type,
                metadata=activity.metadata,
                created_at=activity.created_at,
                updated_at=activity.updated_at
            )
            for activity in activities
        ]
        
        return {
            "created_count": len(activities),
            "activities": activity_responses
        }
        
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("Failed to bulk create activities", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to bulk create activities")


# Health metrics endpoints
@router.get("/squads/{squad_id}/health/score", response_model=HealthScoreResponse)
async def get_squad_health_score(
    squad_id: str,
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    health_service: SquadHealthService = Depends(get_health_service),
    _: None = Depends(verify_api_key)
) -> HealthScoreResponse:
    """Get squad health score."""
    try:
        # Validate squad_id format
        try:
            UUID(squad_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid squad ID format")
        
        health_score = await health_service.calculate_squad_health_score(
            squad_id=squad_id,
            days_to_analyze=days
        )
        
        return HealthScoreResponse(
            health_score=health_score,
            calculated_at=datetime.now(timezone.utc),
            analysis_period_days=days,
            squad_id=squad_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to calculate health score", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to calculate health score")


@router.get("/squads/{squad_id}/health/engagement", response_model=EngagementScoreResponse)
async def get_squad_engagement_score(
    squad_id: str,
    days: int = Query(7, ge=1, le=30, description="Days to analyze"),
    health_service: SquadHealthService = Depends(get_health_service),
    _: None = Depends(verify_api_key)
) -> EngagementScoreResponse:
    """Get squad engagement score."""
    try:
        try:
            UUID(squad_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid squad ID format")
        
        engagement_score = await health_service.calculate_engagement_score(
            squad_id=squad_id,
            days_to_analyze=days
        )
        
        return EngagementScoreResponse(
            engagement_score=engagement_score,
            calculated_at=datetime.now(timezone.utc),
            analysis_period_days=days,
            squad_id=squad_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to calculate engagement score", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to calculate engagement score")


@router.get("/squads/{squad_id}/health/report", response_model=HealthReportResponse)
async def get_squad_health_report(
    squad_id: str,
    include_trends: bool = Query(True, description="Include trend analysis"),
    health_service: SquadHealthService = Depends(get_health_service),
    _: None = Depends(verify_api_key)
) -> HealthReportResponse:
    """Get comprehensive squad health report."""
    try:
        try:
            UUID(squad_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid squad ID format")
        
        report = await health_service.generate_health_report(
            squad_id=squad_id,
            include_trends=include_trends
        )
        
        return HealthReportResponse(
            health_score=report["health_score"],
            engagement_score=report["engagement_score"],
            activity_summary=report["activity_summary"],
            recommendations=report["recommendations"],
            trends=report.get("trends"),
            generated_at=datetime.fromisoformat(report["generated_at"]),
            squad_id=squad_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to generate health report", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to generate health report")


@router.get("/squads/{squad_id}/health/trends")
async def get_squad_activity_trends(
    squad_id: str,
    days: int = Query(30, ge=7, le=365),
    health_service: SquadHealthService = Depends(get_health_service),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Get activity trends analysis."""
    try:
        try:
            UUID(squad_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid squad ID format")
        
        trends = await health_service.analyze_activity_trends(
            squad_id=squad_id,
            days_to_analyze=days
        )
        
        return trends
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to analyze activity trends", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to analyze trends")


@router.get("/squads/{squad_id}/health/patterns")
async def get_squad_activity_patterns(
    squad_id: str,
    pattern_type: str = Query("daily", regex="^(daily|weekly)$"),
    health_service: SquadHealthService = Depends(get_health_service),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Get activity patterns analysis."""
    try:
        try:
            UUID(squad_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid squad ID format")
        
        patterns = await health_service.get_activity_patterns(
            squad_id=squad_id,
            pattern_type=pattern_type
        )
        
        return patterns
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("Failed to get activity patterns", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get patterns")


# Statistics endpoints
@router.get("/guilds/{guild_id}/activities/stats", response_model=ActivityStatisticsResponse)
async def get_guild_activity_statistics(
    guild_id: str,
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> ActivityStatisticsResponse:
    """Get guild activity statistics."""
    try:
        stats = await activity_repo.get_activity_statistics(
            guild_id=guild_id,
            date_from=date_from,
            date_to=date_to
        )
        
        return ActivityStatisticsResponse(
            total_activities=stats["total_activities"],
            activities_by_type=stats["activities_by_type"],
            unique_users=stats["unique_users"],
            date_range=stats["date_range"]
        )
        
    except Exception as e:
        logger.error("Failed to get activity statistics", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get statistics")


@router.get("/guilds/{guild_id}/activities/count")
async def get_activity_count_by_type(
    guild_id: str,
    activity_type: str = Query(..., description="Activity type to count"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Get activity count by type."""
    try:
        count = await activity_repo.get_activity_count_by_type(
            guild_id=guild_id,
            activity_type=activity_type,
            date_from=date_from,
            date_to=date_to
        )
        
        return {
            "count": count,
            "activity_type": activity_type,
            "guild_id": guild_id,
            "date_range": {
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None
            }
        }
        
    except Exception as e:
        logger.error("Failed to get activity count", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get count")


@router.get("/guilds/{guild_id}/activities/recent")
async def get_recent_activities(
    guild_id: str,
    hours: int = Query(24, ge=1, le=168, description="Hours to look back"),
    limit: int = Query(50, ge=1, le=200),
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> Dict[str, Any]:
    """Get recent activities."""
    try:
        activities = await activity_repo.get_recent_activities(
            guild_id=guild_id,
            hours=hours,
            limit=limit
        )
        
        activity_responses = [
            ActivityResponse(
                id=str(activity.id),
                guild_id=activity.guild_id,
                user_id=activity.user_id,
                squad_id=str(activity.squad_id) if activity.squad_id else None,
                activity_type=activity.activity_type,
                metadata=activity.metadata,
                created_at=activity.created_at,
                updated_at=activity.updated_at
            )
            for activity in activities
        ]
        
        return {
            "activities": activity_responses,
            "time_range": {
                "hours_back": hours,
                "from": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            },
            "total_found": len(activities)
        }
        
    except Exception as e:
        logger.error("Failed to get recent activities", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get recent activities")


# Extension points for OCP compliance
@router.get("/version")
async def get_api_version(_: None = Depends(verify_api_key)) -> Dict[str, str]:
    """Get API version information for compatibility."""
    return {
        "version": "1.0.0",
        "api_version": "v1",
        "features": ["activity_tracking", "health_metrics", "analytics"]
    }


@router.post("/webhooks/squad-activity", status_code=200)
async def squad_activity_webhook(
    webhook_data: Dict[str, Any],
    activity_repo: SquadActivityRepository = Depends(get_activity_repository),
    _: None = Depends(verify_api_key)
) -> Dict[str, str]:
    """Extension point for external integrations (OCP compliance)."""
    try:
        # Validate webhook payload
        if "event_type" not in webhook_data or "payload" not in webhook_data:
            raise HTTPException(status_code=422, detail="Invalid webhook payload")
        
        event_type = webhook_data["event_type"]
        payload = webhook_data["payload"]
        
        if event_type == "squad_activity":
            # Create activity from webhook
            await activity_repo.create_activity(
                guild_id=payload.get("guild_id", ""),
                user_id=payload.get("user_id", "webhook_user"),
                activity_type=payload.get("activity_type", "external_integration"),
                squad_id=payload.get("squad_id"),
                metadata=payload.get("metadata", {"source": "webhook"})
            )
            
            return {"status": "processed", "event_type": event_type}
        
        return {"status": "ignored", "reason": f"Unsupported event type: {event_type}"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to process webhook", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to process webhook")