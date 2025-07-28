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
    ErrorResponse
)
from smarter_dev.web.crud import APIKeyOperations
from smarter_dev.web.security import generate_secure_api_key
from smarter_dev.web.models import APIKey as APIKeyModel

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