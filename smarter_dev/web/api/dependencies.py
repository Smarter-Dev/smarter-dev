"""FastAPI dependencies for authentication and database access.

This module provides common dependencies used across API endpoints including
database session management, bot token authentication, and guild access verification.
"""

from __future__ import annotations

from typing import AsyncGenerator, Annotated

from fastapi import Depends, HTTPException, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session

# Security scheme for bearer token authentication
security = HTTPBearer(
    scheme_name="Bearer Token",
    description="Bot token authentication",
    auto_error=True
)


async def get_database_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session.
    
    Provides a database session with automatic transaction management
    and cleanup. Sessions are committed on success and rolled back on error.
    
    Yields:
        AsyncSession: Database session
    """
    async for session in get_db_session():
        yield session


async def verify_bot_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Security(security)]
) -> str:
    """Verify bot token authentication.
    
    Validates the provided bearer token against the configured bot token
    in application settings.
    
    Args:
        request: FastAPI request object to check raw headers
        credentials: HTTP authorization credentials from request header
        
    Returns:
        str: Validated bot token
        
    Raises:
        HTTPException: If token is invalid or missing
    """
    settings = get_settings()
    
    # Check case-sensitive Bearer scheme from raw header
    auth_header = request.headers.get("Authorization")
    if auth_header and not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=403,
            detail="Invalid authorization scheme. Must use 'Bearer' (case-sensitive)",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Extract token from credentials
    token = credentials.credentials
    
    # In a real implementation, this would validate against Discord API
    # For now, we validate against the configured bot token
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bot token is required",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Check if token matches configured bot token
    if settings.discord_bot_token and token != settings.discord_bot_token:
        raise HTTPException(
            status_code=401,
            detail="Invalid bot token",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    return token


async def verify_guild_access(
    request: Request,
    guild_id: str,
    token: Annotated[str, Depends(verify_bot_token)]
) -> str:
    """Verify bot has access to the specified guild.
    
    This dependency ensures that the authenticated bot has access to
    perform operations in the specified guild.
    
    Args:
        request: FastAPI request object
        guild_id: Discord guild snowflake ID from path parameter
        token: Validated bot token from authentication
        
    Returns:
        str: Validated guild ID
        
    Raises:
        HTTPException: If guild access is denied or guild ID is invalid
    """
    # Validate guild ID format (Discord snowflake)
    try:
        guild_id_int = int(guild_id)
        if guild_id_int <= 0:
            raise ValueError("Invalid guild ID")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid guild ID format"
        )
    
    # In a real implementation, this would check if the bot is in the guild
    # via Discord API. For now, we'll do basic validation.
    # This could be enhanced to cache guild membership status.
    
    # Store guild ID in request state for access in endpoints
    request.state.guild_id = guild_id
    
    return guild_id


async def get_current_user_id(
    request: Request,
    token: Annotated[str, Depends(verify_bot_token)]
) -> str:
    """Get current user ID from request context.
    
    This dependency extracts user ID from request headers or context.
    Typically used for user-specific operations.
    
    Args:
        request: FastAPI request object
        token: Validated bot token
        
    Returns:
        str: User ID
        
    Raises:
        HTTPException: If user ID is not found or invalid
    """
    # Extract user ID from custom header
    user_id = request.headers.get("X-User-ID")
    
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="User ID is required in X-User-ID header"
        )
    
    # Validate user ID format (Discord snowflake)
    try:
        user_id_int = int(user_id)
        if user_id_int <= 0:
            raise ValueError("Invalid user ID")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid user ID format"
        )
    
    return user_id


async def get_request_metadata(request: Request) -> dict[str, str]:
    """Extract request metadata for logging and auditing.
    
    Args:
        request: FastAPI request object
        
    Returns:
        dict: Request metadata including IP, user agent, etc.
    """
    return {
        "client_ip": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown"),
        "request_id": request.headers.get("x-request-id", "unknown"),
        "forwarded_for": request.headers.get("x-forwarded-for", "unknown"),
    }


# Type aliases for common dependencies
DatabaseSession = Annotated[AsyncSession, Depends(get_database_session)]
BotToken = Annotated[str, Depends(verify_bot_token)]
GuildAccess = Annotated[str, Depends(verify_guild_access)]
CurrentUser = Annotated[str, Depends(get_current_user_id)]
RequestMetadata = Annotated[dict[str, str], Depends(get_request_metadata)]