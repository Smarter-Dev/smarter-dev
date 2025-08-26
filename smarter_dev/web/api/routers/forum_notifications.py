"""API endpoints for forum notification topics and user subscriptions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4, UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session
from smarter_dev.web.models import ForumNotificationTopic, ForumUserSubscription
from smarter_dev.web.api.dependencies import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["forum-notifications"])


# Pydantic models for API requests/responses
class ForumNotificationTopicCreate(BaseModel):
    topic_name: str
    topic_description: str | None = None


class ForumNotificationTopicResponse(BaseModel):
    id: str | UUID
    guild_id: str
    forum_channel_id: str
    topic_name: str
    topic_description: str | None
    created_at: datetime
    updated_at: datetime


    class Config:
        from_attributes = True


class ForumUserSubscriptionCreate(BaseModel):
    user_id: str
    username: str
    forum_channel_id: str
    subscribed_topics: List[str]
    notification_hours: int


class ForumUserSubscriptionResponse(BaseModel):
    id: str | UUID
    guild_id: str
    user_id: str
    username: str
    forum_channel_id: str
    subscribed_topics: List[str]
    notification_hours: int
    created_at: datetime
    updated_at: datetime


    class Config:
        from_attributes = True


# Topic management endpoints
@router.get(
    "/guilds/{guild_id}/forum-channels/{forum_channel_id}/notification-topics",
    response_model=List[ForumNotificationTopicResponse],
)
async def get_notification_topics(
    guild_id: str,
    forum_channel_id: str,
    db: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key)
):
    """Get all notification topics for a specific forum channel."""
    try:
        stmt = select(ForumNotificationTopic).where(
            and_(
                ForumNotificationTopic.guild_id == guild_id,
                ForumNotificationTopic.forum_channel_id == forum_channel_id
            )
        ).order_by(ForumNotificationTopic.topic_name)
        
        result = await db.execute(stmt)
        topics = result.scalars().all()
        
        return [ForumNotificationTopicResponse.model_validate(topic) for topic in topics]
        
    except Exception as e:
        logger.error(f"Error fetching notification topics for guild {guild_id}, forum {forum_channel_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch notification topics"
        )


# User subscription management endpoints
@router.get(
    "/guilds/{guild_id}/forum-channels/{forum_channel_id}/user-subscriptions",
    response_model=List[ForumUserSubscriptionResponse],
)
async def get_forum_user_subscriptions(
    guild_id: str,
    forum_channel_id: str,
    db: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key)
):
    """Get all user subscriptions for a specific forum channel."""
    try:
        stmt = select(ForumUserSubscription).where(
            and_(
                ForumUserSubscription.guild_id == guild_id,
                ForumUserSubscription.forum_channel_id == forum_channel_id
            )
        ).order_by(ForumUserSubscription.username)
        
        result = await db.execute(stmt)
        subscriptions = result.scalars().all()
        
        # Filter out expired subscriptions
        active_subscriptions = []
        current_time = datetime.now(timezone.utc)
        
        for sub in subscriptions:
            if not sub.is_expired:  # Using the model property
                active_subscriptions.append(sub)
        
        return [ForumUserSubscriptionResponse.model_validate(sub) for sub in active_subscriptions]
        
    except Exception as e:
        logger.error(f"Error fetching user subscriptions for guild {guild_id}, forum {forum_channel_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user subscriptions"
        )


@router.get(
    "/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}",
    response_model=ForumUserSubscriptionResponse,
)
async def get_user_forum_subscription(
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
    db: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key)
):
    """Get a specific user's subscription for a forum channel."""
    try:
        stmt = select(ForumUserSubscription).where(
            and_(
                ForumUserSubscription.guild_id == guild_id,
                ForumUserSubscription.user_id == user_id,
                ForumUserSubscription.forum_channel_id == forum_channel_id
            )
        )
        
        result = await db.execute(stmt)
        subscription = result.scalar_one_or_none()
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User subscription not found"
            )
        
        # Check if expired
        if subscription.is_expired:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User subscription has expired"
            )
        
        return ForumUserSubscriptionResponse.model_validate(subscription)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user subscription: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user subscription"
        )


@router.put(
    "/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}",
    response_model=ForumUserSubscriptionResponse,
)
async def create_or_update_user_forum_subscription(
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
    subscription_data: ForumUserSubscriptionCreate,
    db: AsyncSession = Depends(get_db_session),
    api_key = Depends(verify_api_key)
):
    """Create or update a user's forum subscription."""
    try:
        # Check if subscription already exists
        stmt = select(ForumUserSubscription).where(
            and_(
                ForumUserSubscription.guild_id == guild_id,
                ForumUserSubscription.user_id == user_id,
                ForumUserSubscription.forum_channel_id == forum_channel_id
            )
        )
        
        result = await db.execute(stmt)
        existing_subscription = result.scalar_one_or_none()
        
        current_time = datetime.now(timezone.utc)
        
        if existing_subscription:
            # Update existing subscription
            existing_subscription.username = subscription_data.username
            existing_subscription.subscribed_topics = subscription_data.subscribed_topics
            existing_subscription.notification_hours = subscription_data.notification_hours
            existing_subscription.updated_at = current_time
            
            await db.commit()
            await db.refresh(existing_subscription)
            
            return ForumUserSubscriptionResponse.model_validate(existing_subscription)
            
        else:
            # Create new subscription
            new_subscription = ForumUserSubscription(
                id=uuid4(),
                guild_id=guild_id,
                user_id=subscription_data.user_id,
                username=subscription_data.username,
                forum_channel_id=subscription_data.forum_channel_id,
                subscribed_topics=subscription_data.subscribed_topics,
                notification_hours=subscription_data.notification_hours,
                created_at=current_time,
                updated_at=current_time
            )
            
            db.add(new_subscription)
            await db.commit()
            await db.refresh(new_subscription)
            
            return ForumUserSubscriptionResponse.model_validate(new_subscription)
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Error creating/updating user subscription: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create or update user subscription"
        )