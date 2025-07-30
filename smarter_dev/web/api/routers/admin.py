"""Admin API endpoints for system management.

This module provides REST API endpoints for administrative operations including
API key management, user administration, and system monitoring.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api.dependencies import (
    get_database_session,
    APIKey,
    get_request_metadata
)
from smarter_dev.web.api.schemas import (
    APIKeyCreate,
    APIKeyUpdate,
    APIKeyResponse,
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyRevokeResponse,
    AdminStatsResponse,
    ErrorResponse,
    HelpConversationCreate,
    HelpConversationResponse,
    HelpConversationListResponse,
    HelpConversationCreateResponse,
    HelpConversationStatsResponse
)
from smarter_dev.web.crud import APIKeyOperations
from smarter_dev.web.security import generate_secure_api_key
from smarter_dev.web.models import APIKey as APIKeyModel, HelpConversation

router = APIRouter(prefix="/admin", tags=["admin"])


async def verify_admin_permissions(api_key: APIKey) -> None:
    """Verify that the API key has admin permissions."""
    admin_scopes = {"admin:read", "admin:write", "admin:manage"}
    
    if not any(scope in admin_scopes for scope in api_key.scopes):
        raise HTTPException(
            status_code=403,
            detail="Admin permissions required"
        )


@router.get("/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    request: Request,
    api_key: APIKey,
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> AdminStatsResponse:
    """Get admin dashboard statistics.
    
    Returns overall system statistics including API key usage,
    request counts, and top consumers.
    """
    await verify_admin_permissions(api_key)
    
    api_key_ops = APIKeyOperations()
    
    # Get API key statistics
    stats = await api_key_ops.get_admin_stats(db)
    
    return AdminStatsResponse(
        total_api_keys=stats.get("total_api_keys", 0),
        active_api_keys=stats.get("active_api_keys", 0),
        revoked_api_keys=stats.get("revoked_api_keys", 0),
        expired_api_keys=stats.get("expired_api_keys", 0),
        total_api_requests=stats.get("total_api_requests", 0),
        api_requests_today=stats.get("api_requests_today", 0),
        top_api_consumers=stats.get("top_api_consumers", [])
    )


@router.post("/api-keys", response_model=APIKeyCreateResponse, status_code=201)
async def create_api_key(
    request: Request,
    api_key: APIKey,
    key_data: APIKeyCreate,
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> APIKeyCreateResponse:
    """Create a new API key.
    
    Creates a new secure API key with the specified permissions and settings.
    The full API key is only returned once upon creation.
    """
    await verify_admin_permissions(api_key)
    
    # Generate secure API key
    full_key, key_hash, key_prefix = generate_secure_api_key()
    
    # Create API key record
    new_api_key = APIKeyModel(
        name=key_data.name,
        description=key_data.description,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=key_data.scopes,
        rate_limit_per_hour=key_data.rate_limit_per_hour,
        expires_at=key_data.expires_at,
        created_by=api_key.name,  # Track who created the key
        is_active=True,
        usage_count=0
    )
    
    db.add(new_api_key)
    await db.commit()
    await db.refresh(new_api_key)
    
    # Log API key creation
    from smarter_dev.web.security_logger import get_security_logger
    security_logger = get_security_logger()
    await security_logger.log_api_key_created(
        session=db,
        api_key=new_api_key,
        user_identifier=api_key.name,
        request=request
    )
    
    # Convert to response model
    response_data = APIKeyResponse.model_validate(new_api_key)
    
    # Add the full key (only shown once)
    return APIKeyCreateResponse(
        **response_data.model_dump(),
        api_key=full_key
    )


@router.get("/api-keys", response_model=APIKeyListResponse)
async def list_api_keys(
    request: Request,
    api_key: APIKey,
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Number of items per page"),
    active_only: bool = Query(False, description="Show only active keys"),
    search: Optional[str] = Query(None, description="Search by name or description"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> APIKeyListResponse:
    """List API keys with pagination and filtering.
    
    Returns a paginated list of API keys with metadata.
    Sensitive information like key hashes are not included.
    """
    await verify_admin_permissions(api_key)
    
    # Log admin operation
    from smarter_dev.web.security_logger import get_security_logger
    security_logger = get_security_logger()
    await security_logger.log_admin_operation(
        session=db,
        operation="list_api_keys",
        user_identifier=api_key.name,
        request=request,
        success=True
    )
    
    api_key_ops = APIKeyOperations()
    
    # Calculate offset
    offset = (page - 1) * size
    
    # Get API keys with filters
    keys, total = await api_key_ops.list_api_keys(
        db=db,
        offset=offset,
        limit=size,
        active_only=active_only,
        search=search
    )
    
    # Convert to response models
    key_responses = [APIKeyResponse.model_validate(key) for key in keys]
    
    # Calculate pagination info
    pages = math.ceil(total / size) if total > 0 else 1
    
    return APIKeyListResponse(
        items=key_responses,
        total=total,
        page=page,
        size=size,
        pages=pages
    )


@router.get("/api-keys/{key_id}", response_model=APIKeyResponse)
async def get_api_key(
    request: Request,
    api_key: APIKey,
    key_id: UUID = Path(..., description="API key ID"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> APIKeyResponse:
    """Get details of a specific API key.
    
    Returns detailed information about an API key.
    Sensitive information like the key hash is not included.
    """
    await verify_admin_permissions(api_key)
    
    api_key_ops = APIKeyOperations()
    
    # Get API key by ID
    target_key = await api_key_ops.get_api_key_by_id(session=db, key_id=key_id)
    
    if not target_key:
        raise HTTPException(
            status_code=404,
            detail="API key not found"
        )
    
    return APIKeyResponse.model_validate(target_key)


@router.put("/api-keys/{key_id}", response_model=APIKeyResponse)
async def update_api_key(
    request: Request,
    api_key: APIKey,
    update_data: APIKeyUpdate,
    key_id: UUID = Path(..., description="API key ID"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> APIKeyResponse:
    """Update an existing API key.
    
    Updates the metadata and permissions of an existing API key.
    The actual key value cannot be changed.
    """
    await verify_admin_permissions(api_key)
    
    api_key_ops = APIKeyOperations()
    
    # Get existing API key
    target_key = await api_key_ops.get_api_key_by_id(session=db, key_id=key_id)
    
    if not target_key:
        raise HTTPException(
            status_code=404,
            detail="API key not found"
        )
    
    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    
    for field, value in update_dict.items():
        setattr(target_key, field, value)
    
    # Update modified timestamp
    target_key.updated_at = datetime.now(timezone.utc)
    
    await db.commit()
    await db.refresh(target_key)
    
    return APIKeyResponse.model_validate(target_key)


@router.patch("/api-keys/{key_id}", response_model=APIKeyResponse)
async def partial_update_api_key(
    request: Request,
    api_key: APIKey,
    update_data: APIKeyUpdate,
    key_id: UUID = Path(..., description="API key ID"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> APIKeyResponse:
    """Partially update an existing API key.
    
    Updates only the specified fields of an existing API key.
    The actual key value cannot be changed.
    """
    # Use the same logic as PUT for partial updates
    return await update_api_key(request, api_key, update_data, key_id, db, metadata)


@router.delete("/api-keys/{key_id}", response_model=APIKeyRevokeResponse)
async def revoke_api_key(
    request: Request,
    api_key: APIKey,
    key_id: UUID = Path(..., description="API key ID"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> APIKeyRevokeResponse:
    """Revoke an API key.
    
    Permanently deactivates an API key. This action cannot be undone.
    The key will no longer be able to authenticate API requests.
    """
    await verify_admin_permissions(api_key)
    
    api_key_ops = APIKeyOperations()
    
    # Get existing API key
    target_key = await api_key_ops.get_api_key_by_id(session=db, key_id=key_id)
    
    if not target_key:
        raise HTTPException(
            status_code=404,
            detail="API key not found"
        )
    
    # Check if already revoked
    if not target_key.is_active:
        raise HTTPException(
            status_code=409,
            detail="API key is already revoked"
        )
    
    # Revoke the key
    revoked_at = datetime.now(timezone.utc)
    target_key.is_active = False
    target_key.revoked_at = revoked_at
    target_key.updated_at = revoked_at
    
    await db.commit()
    
    # Log API key deletion
    from smarter_dev.web.security_logger import get_security_logger
    security_logger = get_security_logger()
    await security_logger.log_api_key_deleted(
        session=db,
        api_key_id=key_id,
        api_key_name=target_key.name,
        user_identifier=api_key.name,
        request=request
    )
    
    return APIKeyRevokeResponse(
        message="API key revoked successfully",
        key_id=str(key_id),
        revoked_at=revoked_at
    )


# ============================================================================
# Help Conversation Endpoints
# ============================================================================

@router.post("/conversations", response_model=HelpConversationCreateResponse, status_code=201)
async def create_conversation(
    request: Request,
    api_key: APIKey,
    conversation_data: HelpConversationCreate,
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> HelpConversationCreateResponse:
    """Store a help agent conversation record.
    
    Creates a new conversation record for auditing and analytics purposes.
    Used by the Discord bot to track help agent interactions.
    """
    # Check bot permissions (bot should have write access)
    bot_scopes = {"bot:write", "admin:write"}
    
    if not any(scope in bot_scopes for scope in api_key.scopes):
        raise HTTPException(
            status_code=403,
            detail="Bot write permissions required"
        )
    
    try:
        # Create conversation record
        conversation = HelpConversation(
            session_id=conversation_data.session_id,
            guild_id=conversation_data.guild_id,
            channel_id=conversation_data.channel_id,
            user_id=conversation_data.user_id,
            user_username=conversation_data.user_username,
            interaction_type=conversation_data.interaction_type,
            context_messages=conversation_data.context_messages,
            user_question=conversation_data.user_question,
            bot_response=conversation_data.bot_response,
            tokens_used=conversation_data.tokens_used,
            response_time_ms=conversation_data.response_time_ms,
            retention_policy=conversation_data.retention_policy,
            is_sensitive=conversation_data.is_sensitive
        )
        
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        
        # Log conversation creation
        from smarter_dev.web.security_logger import get_security_logger
        security_logger = get_security_logger()
        await security_logger.log_admin_operation(
            session=db,
            operation="create_help_conversation",
            user_identifier=f"bot:{api_key.name}",
            request=request,
            success=True,
            details=f"Conversation created for user {conversation_data.user_id} in guild {conversation_data.guild_id}"
        )
        
        return HelpConversationCreateResponse(
            id=conversation.id,
            message="Conversation recorded successfully",
            created_at=conversation.created_at
        )
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create conversation record: {str(e)}"
        )


@router.get("/conversations", response_model=HelpConversationListResponse)
async def list_conversations(
    request: Request,
    api_key: APIKey,
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Number of items per page"),
    guild_id: Optional[str] = Query(None, description="Filter by guild ID"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    interaction_type: Optional[str] = Query(None, description="Filter by interaction type"),
    resolved_only: bool = Query(False, description="Show only resolved conversations"),
    search: Optional[str] = Query(None, description="Search in questions and responses"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> HelpConversationListResponse:
    """List help conversations with pagination and filtering.
    
    Returns a paginated list of help agent conversations for admin review.
    Supports filtering by guild, user, interaction type, and text search.
    """
    await verify_admin_permissions(api_key)
    
    try:
        from sqlalchemy import select, func, or_
        
        # Build base query
        query = select(HelpConversation)
        count_query = select(func.count(HelpConversation.id))
        
        # Apply filters
        if guild_id:
            query = query.where(HelpConversation.guild_id == guild_id)
            count_query = count_query.where(HelpConversation.guild_id == guild_id)
        
        if user_id:
            query = query.where(HelpConversation.user_id == user_id)
            count_query = count_query.where(HelpConversation.user_id == user_id)
            
        if interaction_type:
            query = query.where(HelpConversation.interaction_type == interaction_type)
            count_query = count_query.where(HelpConversation.interaction_type == interaction_type)
            
        if resolved_only:
            query = query.where(HelpConversation.is_resolved == True)
            count_query = count_query.where(HelpConversation.is_resolved == True)
            
        if search:
            search_filter = or_(
                HelpConversation.user_question.ilike(f"%{search}%"),
                HelpConversation.bot_response.ilike(f"%{search}%"),
                HelpConversation.user_username.ilike(f"%{search}%")
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)
        
        # Apply pagination and ordering
        offset = (page - 1) * size
        query = query.order_by(HelpConversation.started_at.desc()).offset(offset).limit(size)
        
        # Execute queries
        result = await db.execute(query)
        conversations = result.scalars().all()
        
        count_result = await db.execute(count_query)
        total = count_result.scalar()
        
        # Convert to response models
        conversation_responses = [HelpConversationResponse.model_validate(conv) for conv in conversations]
        
        # Calculate pagination info
        pages = math.ceil(total / size) if total > 0 else 1
        
        # Log admin operation
        from smarter_dev.web.security_logger import get_security_logger
        security_logger = get_security_logger()
        await security_logger.log_admin_operation(
            session=db,
            operation="list_help_conversations",
            user_identifier=api_key.name,
            request=request,
            success=True,
            details=f"Listed {len(conversations)} conversations (page {page}/{pages})"
        )
        
        return HelpConversationListResponse(
            items=conversation_responses,
            total=total,
            page=page,
            size=size,
            pages=pages
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list conversations: {str(e)}"
        )


@router.get("/conversations/{conversation_id}", response_model=HelpConversationResponse)
async def get_conversation(
    request: Request,
    api_key: APIKey,
    conversation_id: UUID = Path(..., description="Conversation ID"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> HelpConversationResponse:
    """Get details of a specific help conversation.
    
    Returns complete conversation details including context messages,
    question, response, and performance metrics.
    """
    await verify_admin_permissions(api_key)
    
    try:
        from sqlalchemy import select
        
        # Get conversation by ID
        query = select(HelpConversation).where(HelpConversation.id == conversation_id)
        result = await db.execute(query)
        conversation = result.scalar_one_or_none()
        
        if not conversation:
            raise HTTPException(
                status_code=404,
                detail="Conversation not found"
            )
        
        # Log admin operation
        from smarter_dev.web.security_logger import get_security_logger
        security_logger = get_security_logger()
        await security_logger.log_admin_operation(
            session=db,
            operation="view_help_conversation",
            user_identifier=api_key.name,
            request=request,
            success=True,
            details=f"Viewed conversation {conversation_id}"
        )
        
        return HelpConversationResponse.model_validate(conversation)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get conversation: {str(e)}"
        )


@router.get("/conversations/stats", response_model=HelpConversationStatsResponse)
async def get_conversation_stats(
    request: Request,
    api_key: APIKey,
    guild_id: Optional[str] = Query(None, description="Filter stats by guild ID"),
    days: int = Query(30, ge=1, le=365, description="Number of days to include in stats"),
    db: AsyncSession = Depends(get_database_session),
    metadata: dict = Depends(get_request_metadata)
) -> HelpConversationStatsResponse:
    """Get help conversation statistics.
    
    Returns analytics data including conversation counts, token usage,
    response times, and user engagement metrics.
    """
    await verify_admin_permissions(api_key)
    
    try:
        from sqlalchemy import select, func
        from datetime import timedelta
        
        # Calculate date range
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
        today_start = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Base query filters
        base_filter = HelpConversation.started_at >= start_date
        today_filter = HelpConversation.started_at >= today_start
        
        if guild_id:
            base_filter = base_filter & (HelpConversation.guild_id == guild_id)
            today_filter = today_filter & (HelpConversation.guild_id == guild_id)
        
        # Total conversations
        total_query = select(func.count(HelpConversation.id)).where(base_filter)
        total_result = await db.execute(total_query)
        total_conversations = total_result.scalar() or 0
        
        # Conversations today
        today_query = select(func.count(HelpConversation.id)).where(today_filter)
        today_result = await db.execute(today_query)
        conversations_today = today_result.scalar() or 0
        
        # Token usage
        tokens_query = select(func.sum(HelpConversation.tokens_used)).where(base_filter)
        tokens_result = await db.execute(tokens_query)
        total_tokens = tokens_result.scalar() or 0
        
        tokens_today_query = select(func.sum(HelpConversation.tokens_used)).where(today_filter)
        tokens_today_result = await db.execute(tokens_today_query)
        tokens_today = tokens_today_result.scalar() or 0
        
        # Average response time
        avg_time_query = select(func.avg(HelpConversation.response_time_ms)).where(
            base_filter & (HelpConversation.response_time_ms.is_not(None))
        )
        avg_time_result = await db.execute(avg_time_query)
        avg_response_time = avg_time_result.scalar()
        avg_response_time_ms = int(avg_response_time) if avg_response_time else None
        
        # Top users
        top_users_query = select(
            HelpConversation.user_username,
            HelpConversation.user_id,
            func.count(HelpConversation.id).label('conversation_count'),
            func.sum(HelpConversation.tokens_used).label('total_tokens')
        ).where(base_filter).group_by(
            HelpConversation.user_username, HelpConversation.user_id
        ).order_by(func.count(HelpConversation.id).desc()).limit(10)
        
        top_users_result = await db.execute(top_users_query)
        top_users = [
            {
                "username": row.user_username,
                "user_id": row.user_id,
                "conversation_count": row.conversation_count,
                "total_tokens": row.total_tokens or 0
            }
            for row in top_users_result
        ]
        
        # Conversation types breakdown
        types_query = select(
            HelpConversation.interaction_type,
            func.count(HelpConversation.id).label('count')
        ).where(base_filter).group_by(HelpConversation.interaction_type)
        
        types_result = await db.execute(types_query)
        conversation_types = {row.interaction_type: row.count for row in types_result}
        
        # Resolution rate
        resolved_query = select(func.count(HelpConversation.id)).where(
            base_filter & (HelpConversation.is_resolved == True)
        )
        resolved_result = await db.execute(resolved_query)
        resolved_count = resolved_result.scalar() or 0
        
        resolution_rate = (resolved_count / total_conversations * 100) if total_conversations > 0 else 0.0
        
        # Log admin operation
        from smarter_dev.web.security_logger import get_security_logger
        security_logger = get_security_logger()
        await security_logger.log_admin_operation(
            session=db,
            operation="view_help_conversation_stats",
            user_identifier=api_key.name,
            request=request,
            success=True,
            details=f"Viewed conversation stats for {days} days" + (f" in guild {guild_id}" if guild_id else "")
        )
        
        return HelpConversationStatsResponse(
            total_conversations=total_conversations,
            conversations_today=conversations_today,
            total_tokens_used=total_tokens,
            tokens_used_today=tokens_today,
            average_response_time_ms=avg_response_time_ms,
            top_users=top_users,
            conversation_types=conversation_types,
            resolution_rate=resolution_rate
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get conversation stats: {str(e)}"
        )