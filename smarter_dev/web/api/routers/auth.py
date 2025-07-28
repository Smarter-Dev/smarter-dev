"""Authentication endpoints for the Smarter Dev API.

This module provides authentication and authorization endpoints for validating
bot tokens and managing API access.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from smarter_dev.shared.config import get_settings
from smarter_dev.web.api.dependencies import verify_api_key, APIKey, RequestMetadata
from smarter_dev.web.api.schemas import TokenResponse, HealthResponse

router = APIRouter()
security = HTTPBearer()


@router.post("/validate", response_model=TokenResponse)
async def validate_token(
    api_key: APIKey,
    metadata: RequestMetadata
) -> TokenResponse:
    """Validate an API key.
    
    This endpoint validates the provided API key and returns validation status.
    Useful for checking key validity before making other API calls.
    
    Args:
        api_key: Validated API key from dependency
        metadata: Request metadata for logging
        
    Returns:
        TokenResponse: Token validation result
    """
    # If we reach here, API key is valid (dependency would have raised HTTPException)
    return TokenResponse(
        valid=True,
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None
    )


@router.get("/health", response_model=HealthResponse)
async def auth_health_check() -> HealthResponse:
    """Health check for authentication service.
    
    Returns the health status of the authentication system and its dependencies.
    
    Returns:
        HealthResponse: Authentication service health status
    """
    settings = get_settings()
    
    # Check if bot token is configured
    token_configured = bool(settings.discord_bot_token)
    
    return HealthResponse(
        status="healthy" if token_configured else "degraded",
        version="1.0.0",
        timestamp=datetime.now(timezone.utc),
        database=True,  # Will be updated with actual database check
        redis=True      # Will be updated with actual redis check
    )


@router.get("/status")
async def auth_status(api_key: APIKey) -> dict[str, Any]:
    """Get authentication status for the current API key.
    
    Returns information about the authenticated API key and its permissions.
    
    Args:
        api_key: Validated API key
        
    Returns:
        dict: Authentication status information
    """
    settings = get_settings()
    
    return {
        "authenticated": True,
        "key_name": api_key.name,
        "key_prefix": api_key.key_prefix,
        "scopes": api_key.scopes,
        "usage_count": api_key.usage_count,
        "rate_limit": api_key.rate_limit_per_hour,
        "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        "environment": settings.environment,
        "api_version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }