"""Security utilities for API error handling and response sanitization.

This module provides utilities for creating standardized error responses
that avoid information disclosure while maintaining proper logging.
"""

from __future__ import annotations

from fastapi import HTTPException
from smarter_dev.shared.config import get_settings


def create_generic_error_response(
    status_code: int,
    generic_message: str,
    detailed_message: str | None = None,
    exception: Exception | None = None
) -> HTTPException:
    """Create a generic error response that hides details in production.
    
    Args:
        status_code: HTTP status code
        generic_message: Generic error message for production
        detailed_message: Detailed message for development (optional)
        exception: Original exception for development details (optional)
        
    Returns:
        HTTPException: Configured exception with appropriate detail level
    """
    settings = get_settings()
    
    if settings.verbose_errors_enabled and settings.is_development:
        # Show detailed errors in development
        if detailed_message:
            detail = detailed_message
        elif exception:
            detail = f"{generic_message}: {str(exception)}"
        else:
            detail = generic_message
    else:
        # Use generic message in production
        detail = generic_message
    
    return HTTPException(status_code=status_code, detail=detail)


def create_database_error(exception: Exception) -> HTTPException:
    """Create a standardized database error response.
    
    Args:
        exception: Database exception
        
    Returns:
        HTTPException: Configured database error
    """
    return create_generic_error_response(
        status_code=500,
        generic_message="Internal server error",
        detailed_message=f"Database error: {str(exception)}",
        exception=exception
    )


def create_validation_error(message: str = "Invalid request") -> HTTPException:
    """Create a standardized validation error response.
    
    Args:
        message: Validation error message
        
    Returns:
        HTTPException: Configured validation error
    """
    return create_generic_error_response(
        status_code=400,
        generic_message="Invalid request",
        detailed_message=message
    )


def create_not_found_error(resource: str = "Resource") -> HTTPException:
    """Create a standardized not found error response.
    
    Args:
        resource: Name of resource that was not found
        
    Returns:
        HTTPException: Configured not found error
    """
    settings = get_settings()
    
    if settings.verbose_errors_enabled and settings.is_development:
        detail = f"{resource} not found"
    else:
        detail = "Not found"
    
    return HTTPException(status_code=404, detail=detail)


def create_authentication_error(reason: str | None = None) -> HTTPException:
    """Create a standardized authentication error response.
    
    Args:
        reason: Detailed reason for development (optional)
        
    Returns:
        HTTPException: Configured authentication error
    """
    settings = get_settings()
    
    if settings.verbose_errors_enabled and settings.is_development and reason:
        detail = reason
    else:
        detail = "Authentication failed"
    
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"}
    )


def create_authorization_error(reason: str | None = None) -> HTTPException:
    """Create a standardized authorization error response.
    
    Args:
        reason: Detailed reason for development (optional)
        
    Returns:
        HTTPException: Configured authorization error
    """
    settings = get_settings()
    
    if settings.verbose_errors_enabled and settings.is_development and reason:
        detail = reason
    else:
        detail = "Access denied"
    
    return HTTPException(status_code=403, detail=detail)


def create_conflict_error(message: str = "Conflict") -> HTTPException:
    """Create a standardized conflict error response.
    
    Args:
        message: Conflict error message
        
    Returns:
        HTTPException: Configured conflict error
    """
    return create_generic_error_response(
        status_code=409,
        generic_message="Conflict",
        detailed_message=message
    )


def create_rate_limit_error(message: str = "Rate limit exceeded") -> HTTPException:
    """Create a standardized rate limit error response.
    
    Args:
        message: Rate limit error message
        
    Returns:
        HTTPException: Configured rate limit error
    """
    return create_generic_error_response(
        status_code=429,
        generic_message="Too many requests",
        detailed_message=message
    )