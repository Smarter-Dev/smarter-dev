"""FastAPI dependencies for authentication and database access.

This module provides common dependencies used across API endpoints including
database session management, bot token authentication, and guild access verification.

API key verification is dual-source during the legacy sunset:

- Skrift-native ``sk_`` keys are verified against the main DB (``skrift``
  schema) via :mod:`skrift.db.services.api_key_service`.
- Legacy ``sk-`` keys fall back to the legacy ``public.api_keys`` table.

Both branches return an :class:`AuthenticatedKey` so downstream consumers
(rate limiter, security logger, routers) never need to know which table the
key came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Annotated
from uuid import UUID as UUIDType

from fastapi import Depends, HTTPException, Security, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.services import api_key_service as skrift_api_key_service

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session, get_skrift_db_session_context
from smarter_dev.web.security import (
    SKRIFT_API_KEY_PREFIX,
    hash_api_key,
    validate_api_key_format,
)

logger = logging.getLogger(__name__)

# Security scheme for bearer token authentication
security = HTTPBearer(
    scheme_name="Bearer Token",
    description="Bot token authentication",
    auto_error=True
)

# Rate-limit windows applied to Skrift-native keys. The Skrift api_keys table
# carries no per-window limits, so Skrift keys get the same defaults the
# legacy table applied to new rows (models.APIKey column defaults).
SKRIFT_KEY_RATE_LIMIT_PER_SECOND = 10
SKRIFT_KEY_RATE_LIMIT_PER_MINUTE = 180
SKRIFT_KEY_RATE_LIMIT_PER_15_MINUTES = 2500
SKRIFT_KEY_RATE_LIMIT_PER_HOUR = 10000


@dataclass
class AuthenticatedKey:
    """Source-agnostic view of a verified API key.

    Produced by both the Skrift-native and legacy verification branches so
    that downstream consumers (routers, rate limiter, security logger) keep a
    single contract regardless of which key table authenticated the request.
    """

    id: UUIDType
    name: str
    key_prefix: str
    created_by: str
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    usage_count: int = 0
    rate_limit_per_second: int = SKRIFT_KEY_RATE_LIMIT_PER_SECOND
    rate_limit_per_minute: int = SKRIFT_KEY_RATE_LIMIT_PER_MINUTE
    rate_limit_per_15_minutes: int = SKRIFT_KEY_RATE_LIMIT_PER_15_MINUTES
    rate_limit_per_hour: int = SKRIFT_KEY_RATE_LIMIT_PER_HOUR
    is_legacy: bool = False
    is_active: bool = True

    @property
    def is_expired(self) -> bool:
        """Check whether the key's expiry timestamp has passed."""
        if self.expires_at is None:
            return False
        expires = self.expires_at
        # Handle timezone-naive datetimes (e.g. from SQLite) by assuming UTC
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expires

    @property
    def is_valid(self) -> bool:
        """Check whether the key is active and not expired."""
        return self.is_active and not self.is_expired


def authenticated_key_from_legacy(legacy_api_key) -> AuthenticatedKey:
    """Build the source-agnostic key view from a legacy APIKey row."""
    return AuthenticatedKey(
        id=legacy_api_key.id,
        name=legacy_api_key.name,
        key_prefix=legacy_api_key.key_prefix,
        created_by=legacy_api_key.created_by,
        scopes=list(legacy_api_key.scopes or []),
        expires_at=legacy_api_key.expires_at,
        usage_count=legacy_api_key.usage_count,
        rate_limit_per_second=legacy_api_key.rate_limit_per_second,
        rate_limit_per_minute=legacy_api_key.rate_limit_per_minute,
        rate_limit_per_15_minutes=legacy_api_key.rate_limit_per_15_minutes,
        rate_limit_per_hour=legacy_api_key.rate_limit_per_hour,
        is_legacy=True,
    )


def authenticated_key_from_skrift(skrift_api_key) -> AuthenticatedKey:
    """Build the source-agnostic key view from a Skrift APIKey row."""
    principal_name = (
        skrift_api_key.service_name
        or skrift_api_key.display_name
        or str(skrift_api_key.user_id)
    )
    return AuthenticatedKey(
        id=skrift_api_key.id,
        name=skrift_api_key.display_name,
        key_prefix=skrift_api_key.key_prefix,
        created_by=principal_name,
        scopes=list(skrift_api_key.scoped_permission_list),
        expires_at=skrift_api_key.expires_at,
        is_legacy=False,
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


def _authentication_failed_error(verbose_detail: str) -> HTTPException:
    """Build a 401 with a generic message outside verbose development mode."""
    settings = get_settings()
    if settings.verbose_errors_enabled and settings.is_development:
        detail = verbose_detail
    else:
        detail = "Authentication failed"

    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"}
    )


async def _log_authentication_failed(
    request: Request,
    failed_key_prefix: str,
    reason: str
) -> None:
    """Record a failed authentication attempt without breaking the request."""
    from smarter_dev.web.security_logger import get_security_logger

    try:
        await get_security_logger().log_authentication_failed(
            session=None,  # Use separate session for reliability
            failed_key_prefix=failed_key_prefix,
            request=request,
            reason=reason
        )
    except Exception as log_error:
        # Don't let logging errors break authentication
        logger.warning(f"Failed to log authentication failure: {log_error}")


async def _verify_skrift_api_key(request: Request, token: str) -> AuthenticatedKey:
    """Verify an ``sk_`` token against the Skrift-native api_keys table.

    Opens a short-lived main-DB (skrift schema) session for the lookup; the
    request-scoped legacy session stays untouched for downstream consumers.
    """
    client_ip = request.client.host if request.client else None

    async with get_skrift_db_session_context() as skrift_session:
        skrift_api_key = await skrift_api_key_service.verify_api_key(
            skrift_session,
            token,
            client_ip=client_ip,
        )
        if skrift_api_key is None:
            await _log_authentication_failed(
                request,
                failed_key_prefix=token[:10],
                reason="Invalid, revoked, or expired Skrift API key"
            )
            raise _authentication_failed_error("Invalid or revoked API key")

        return authenticated_key_from_skrift(skrift_api_key)


async def _verify_legacy_api_key(
    request: Request,
    token: str,
    session: AsyncSession
) -> AuthenticatedKey:
    """Verify an ``sk-`` token against the legacy public.api_keys table.

    LEGACY-FALLBACK: remove after key rotation
    (see docs/v2/legacy-sunset/runbooks/01-key-rotation.md).
    """
    from smarter_dev.web.crud import APIKeyOperations

    token_hash = hash_api_key(token)
    api_key = await APIKeyOperations().get_api_key_by_hash(session, token_hash)

    if not api_key:
        await _log_authentication_failed(
            request,
            failed_key_prefix=token[:10],
            reason="Invalid or revoked API key"
        )
        raise _authentication_failed_error("Invalid or revoked API key")

    # Check if key is expired (additional security check)
    if api_key.is_expired:
        await _log_authentication_failed(
            request,
            failed_key_prefix=api_key.key_prefix,
            reason="API key has expired"
        )
        raise _authentication_failed_error("API key has expired")

    # Observable counter for the rotation soak window: once this line stops
    # appearing in prod logs, the fallback (and legacy key rows) can go.
    # Only the 12-char display prefix is logged, never the key material —
    # the same disclosure the security_logs table already records.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.info(
        "legacy-api-key-auth: legacy sk- key '%s***' authenticated",
        api_key.key_prefix,
    )

    return authenticated_key_from_legacy(api_key)


async def verify_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Security(security)],
    session: Annotated[AsyncSession, Depends(get_database_session)]
) -> AuthenticatedKey:
    """Verify API key authentication with cryptographic security.

    Skrift-native ``sk_`` keys are verified against the main DB first;
    legacy ``sk-`` keys fall back to the legacy api_keys table. Both
    branches return the same :class:`AuthenticatedKey` contract.

    Args:
        request: FastAPI request object
        credentials: HTTP authorization credentials from request header
        session: Legacy database session, kept as the request session for
            downstream consumers (rate limiter, security logs) in both branches

    Returns:
        AuthenticatedKey: Source-agnostic view of the validated key

    Raises:
        HTTPException: If key is invalid, expired, or revoked
    """
    from smarter_dev.web.security_logger import get_security_logger

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
        raise _authentication_failed_error("Invalid API key format")

    if token.startswith(SKRIFT_API_KEY_PREFIX):
        authenticated_key = await _verify_skrift_api_key(request, token)
    else:
        # LEGACY-FALLBACK: remove after key rotation (see runbook
        # docs/v2/legacy-sunset/runbooks/01-key-rotation.md)
        authenticated_key = await _verify_legacy_api_key(request, token, session)

    # Log successful API key usage against the legacy security_logs table
    await get_security_logger().log_api_key_used(
        session=session,
        api_key=authenticated_key,
        request=request,
        success=True
    )

    # Store API key in request state for use by other dependencies.
    # request.state.db_session stays the legacy session in both branches:
    # its consumers (multi-tier rate limiter) read/write legacy-DB tables.
    request.state.api_key = authenticated_key
    request.state.db_session = session

    # Note: Usage tracking is now handled by the rate limiter
    # to avoid duplicate database operations

    return authenticated_key


async def verify_guild_access(
    request: Request,
    guild_id: str,
    api_key: Annotated[AuthenticatedKey, Depends(verify_api_key)]
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
    api_key: Annotated[AuthenticatedKey, Depends(verify_api_key)]
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
APIKey = Annotated[AuthenticatedKey, Depends(verify_api_key)]
GuildAccess = Annotated[str, Depends(verify_guild_access)]
CurrentUser = Annotated[str, Depends(get_current_user_id)]
RequestMetadata = Annotated[dict[str, str], Depends(get_request_metadata)]