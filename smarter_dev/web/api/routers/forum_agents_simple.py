"""Simplified forum agents API endpoints for the Smarter Dev API."""

from __future__ import annotations

from typing import List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import (
    get_database_session,
    verify_guild_access
)
from smarter_dev.web.api.exceptions import (
    create_not_found_error,
    validate_discord_id
)
from smarter_dev.web.crud import ForumAgentOperations
from smarter_dev.web.models import ForumAgentResponse

router = APIRouter(prefix="/guilds/{guild_id}/forum-agents", tags=["forum-agents"])


@router.get("", response_model=List[dict])
async def get_forum_agents(
    request: Request,
    guild_id: str,
    db: AsyncSession = Depends(get_database_session)
) -> List[dict]:
    """Get all active forum agents for a guild."""
    # Basic validation
    validate_discord_id(guild_id, "guild_id")
    
    try:
        forum_ops = ForumAgentOperations(db)
        agents = await forum_ops.list_agents(guild_id)
        
        # Convert to dict format
        agent_data = []
        for agent in agents:
            agent_dict = {
                "id": str(agent.id),
                "guild_id": guild_id,
                "name": agent.name,
                "description": agent.description,
                "system_prompt": agent.system_prompt,
                "monitored_forums": agent.monitored_forums,
                "is_active": agent.is_active,
                "enable_responses": agent.enable_responses,
                "enable_user_tagging": agent.enable_user_tagging,
                "response_threshold": agent.response_threshold,
                "max_responses_per_hour": agent.max_responses_per_hour,
                "created_by": agent.created_by,
                "created_at": agent.created_at.isoformat() if agent.created_at else None,
                "updated_at": agent.updated_at.isoformat() if agent.updated_at else None
            }
            agent_data.append(agent_dict)
        
        return agent_data
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve forum agents: {str(e)}"
        )


@router.post("/{agent_id}/responses", response_model=dict)
async def record_agent_response(
    request: Request,
    guild_id: str,
    agent_id: UUID,
    response_data: dict,
    db: AsyncSession = Depends(get_database_session)
) -> dict:
    """Record a forum agent response."""
    validate_discord_id(guild_id, "guild_id")
    
    try:
        forum_ops = ForumAgentOperations(db)
        
        # Verify agent exists and belongs to guild
        agent = await forum_ops.get_agent(agent_id, guild_id)
        if not agent:
            raise create_not_found_error("Forum agent not found")
        
        # Create the response record
        from datetime import datetime, timezone
        from uuid import uuid4
        
        response_record = ForumAgentResponse(
            id=uuid4(),
            agent_id=agent_id,
            guild_id=guild_id,  # Add the missing guild_id
            channel_id=response_data.get('channel_id', ''),
            thread_id=response_data.get('thread_id', ''),
            post_title=response_data.get('post_title', ''),
            post_content=response_data.get('post_content', ''),
            author_display_name=response_data.get('author_display_name', 'Unknown'),
            post_tags=response_data.get('post_tags', []),
            attachments=response_data.get('attachments', []),
            decision_reason=response_data.get('decision_reason', ''),
            confidence_score=response_data.get('confidence_score', 0.0),
            response_content=response_data.get('response_content', ''),
            tokens_used=response_data.get('tokens_used', 0),
            response_time_ms=response_data.get('response_time_ms', 0),
            responded=response_data.get('responded', False),
            created_at=datetime.now(timezone.utc)
        )
        
        db.add(response_record)
        await db.commit()
        await db.refresh(response_record)
        
        return {"id": str(response_record.id), "created_at": response_record.created_at.isoformat()}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to record agent response: {str(e)}"
        )


@router.get("/{agent_id}/responses/count", response_model=dict)
async def get_agent_response_count(
    request: Request,
    guild_id: str,
    agent_id: UUID,
    hours: int = 1,
    db: AsyncSession = Depends(get_database_session)
) -> dict:
    """Get the count of agent responses within a time period."""
    validate_discord_id(guild_id, "guild_id")
    
    try:
        forum_ops = ForumAgentOperations(db)
        
        # Verify agent exists and belongs to guild
        agent = await forum_ops.get_agent(agent_id, guild_id)
        if not agent:
            raise create_not_found_error("Forum agent not found")
        
        # Count responses in the specified time period
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select, and_, func
        
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        stmt = select(func.count(ForumAgentResponse.id)).where(
            and_(
                ForumAgentResponse.agent_id == agent_id,
                ForumAgentResponse.responded == True,  # Only count actual responses
                ForumAgentResponse.created_at >= cutoff_time
            )
        )
        
        result = await db.execute(stmt)
        count = result.scalar()
        
        return {"count": count or 0, "hours": hours, "cutoff_time": cutoff_time.isoformat()}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get agent response count: {str(e)}"
        )