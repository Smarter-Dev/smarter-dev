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
from smarter_dev.web.api.dependencies import verify_bot_token, BotToken, RequestMetadata
from smarter_dev.web.api.schemas import TokenResponse, HealthResponse

router = APIRouter()
security = HTTPBearer()


@router.post("/validate", response_model=TokenResponse)
async def validate_token(
    token: BotToken,
    metadata: RequestMetadata
) -> TokenResponse:
    """Validate a bot token.
    
    This endpoint validates the provided bot token and returns validation status.
    Useful for checking token validity before making other API calls.
    
    Args:
        token: Validated bot token from dependency
        metadata: Request metadata for logging
        
    Returns:
        TokenResponse: Token validation result
    """
    # If we reach here, token is valid (dependency would have raised HTTPException)
    return TokenResponse(
        valid=True,
        expires_at=None  # Bot tokens don't expire in this implementation
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
async def auth_status(token: BotToken) -> dict[str, Any]:
    """Get authentication status for the current token.
    
    Returns information about the authenticated token and its permissions.
    
    Args:
        token: Validated bot token
        
    Returns:
        dict: Authentication status information
    """
    settings = get_settings()
    
    return {
        "authenticated": True,
        "token_type": "bot",
        "environment": settings.environment,
        "api_version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }