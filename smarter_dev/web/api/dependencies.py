"""FastAPI dependencies for authentication and database access.

This module provides common dependencies used across API endpoints including
database session management, bot token authentication, and guild access verification.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Annotated

from fastapi import Depends, HTTPException, Security, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session

logger = logging.getLogger(__name__)

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


async def verify_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Security(security)],
    session: Annotated[AsyncSession, Depends(get_database_session)]
) -> "APIKey":
    """Verify API key authentication with cryptographic security.
    
    Validates the provided API key using secure hashing and database lookup.
    Implements constant-time comparison to prevent timing attacks.
    
    Args:
        request: FastAPI request object
        credentials: HTTP authorization credentials from request header
        session: Database session for key lookup
        
    Returns:
        APIKey: Validated API key model with metadata
        
    Raises:
        HTTPException: If key is invalid, expired, or revoked
    """
    from smarter_dev.web.security import validate_api_key_format, hash_api_key
    from smarter_dev.web.crud import APIKeyOperations
    import asyncio
    
    # Check case-sensitive Bearer scheme from raw header
    auth_header = request.headers.get("Authorization")
    if auth_header and not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Extract token from credentials
    token = credentials.credentials
    
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Validate API key format (constant-time)
    if not validate_api_key_format(token):
        # Use generic error message to avoid information disclosure
        settings = get_settings()
        if settings.verbose_errors_enabled and settings.is_development:
            detail = "Invalid API key format"
        else:
            detail = "Authentication failed"
        
        raise HTTPException(
            status_code=401,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Hash the token for database lookup
    token_hash = hash_api_key(token)
    
    # Database lookup for API key
    api_key_ops = APIKeyOperations()
    api_key = await api_key_ops.get_api_key_by_hash(session, token_hash)
    
    # Import security logger here to avoid circular imports
    from smarter_dev.web.security_logger import get_security_logger
    security_logger = get_security_logger()
    
    if not api_key:
        # Log failed authentication attempt using a separate session
        try:
            await security_logger.log_authentication_failed(
                session=None,  # Use separate session for reliability
                failed_key_prefix=token[:10] if len(token) >= 10 else token,
                request=request,
                reason="Invalid or revoked API key"
            )
        except Exception as log_error:
            # Don't let logging errors break authentication
            logger.warning(f"Failed to log authentication failure: {log_error}")
        
        # Use generic error message
        settings = get_settings()
        if settings.verbose_errors_enabled and settings.is_development:
            detail = "Invalid or revoked API key"
        else:
            detail = "Authentication failed"
            
        raise HTTPException(
            status_code=401,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Check if key is expired (additional security check)
    if api_key.is_expired:
        # Log expired key usage attempt
        await security_logger.log_authentication_failed(
            session=None,  # Use separate session for reliability
            failed_key_prefix=api_key.key_prefix,
            request=request,
            reason="API key has expired"
        )
        # Use generic error message
        settings = get_settings()
        if settings.verbose_errors_enabled and settings.is_development:
            detail = "API key has expired"
        else:
            detail = "Authentication failed"
            
        raise HTTPException(
            status_code=401,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Log successful API key usage
    await security_logger.log_api_key_used(
        session=session,
        api_key=api_key,
        request=request,
        success=True
    )
    
    # Store API key in request state for use by other dependencies
    request.state.api_key = api_key
    request.state.db_session = session
    
    # Note: Usage tracking is now handled by the rate limiter
    # to avoid duplicate database operations
    
    return api_key


async def verify_guild_access(
    request: Request,
    guild_id: str,
    api_key: Annotated["APIKey", Depends(verify_api_key)]
) -> str:
    """Verify bot has access to the specified guild.
    
    This dependency ensures that the authenticated bot has access to
    perform operations in the specified guild.
    
    Args:
        request: FastAPI request object
        guild_id: Discord guild snowflake ID from path parameter
        api_key: Validated API key from authentication
        
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
            detail="Invalid guild ID"
        )
    
    # In a real implementation, this would check if the bot is in the guild
    # via Discord API. For now, we'll do basic validation.
    # This could be enhanced to cache guild membership status.
    
    # Store guild ID in request state for access in endpoints
    request.state.guild_id = guild_id
    
    return guild_id


async def get_current_user_id(
    request: Request,
    api_key: Annotated["APIKey", Depends(verify_api_key)]
) -> str:
    """Get current user ID from request context.
    
    This dependency extracts user ID from request headers or context.
    Typically used for user-specific operations.
    
    Args:
        request: FastAPI request object
        api_key: Validated API key
        
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
            detail="User ID required"
        )
    
    # Validate user ID format (Discord snowflake)
    try:
        user_id_int = int(user_id)
        if user_id_int <= 0:
            raise ValueError("Invalid user ID")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid user ID"
        )
    
    return user_id


async def apply_rate_limiting(
    request: Request,
    response: Response
) -> None:
    """Apply multi-tier rate limiting to authenticated requests.
    
    This dependency enforces multiple rate limiting windows:
    - 10 requests per second (burst protection)
    - 180 requests per minute (short-term abuse prevention) 
    - 2500 requests per 15 minutes (sustained abuse prevention)
    
    Args:
        request: FastAPI request object
        response: FastAPI response object
        
    Raises:
        HTTPException: If any rate limit is exceeded (429)
    """
    from smarter_dev.web.multi_tier_rate_limiter import enforce_multi_tier_rate_limits
    
    # Get API key and session from request state
    api_key = getattr(request.state, "api_key", None)
    session = getattr(request.state, "db_session", None)
    
    if api_key and session:
        # Enforce multi-tier rate limiting
        await enforce_multi_tier_rate_limits(api_key, session, request, response)


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
APIKey = Annotated["APIKey", Depends(verify_api_key)]
GuildAccess = Annotated[str, Depends(verify_guild_access)]
CurrentUser = Annotated[str, Depends(get_current_user_id)]
RequestMetadata = Annotated[dict[str, str], Depends(get_request_metadata)]