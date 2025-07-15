"""Common exception utilities for API endpoints.

This module provides utilities for consistent error handling across all API endpoints,
including standardized HTTP status codes and error response helpers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from smarter_dev.web.api.schemas import ErrorResponse


def create_http_exception(
    status_code: int,
    detail: str,
    error_type: str,
    request: Optional[Request] = None
) -> HTTPException:
    """Create a standardized HTTPException with proper error format.
    
    Args:
        status_code: HTTP status code
        detail: Error message
        error_type: Error type identifier
        request: Optional request object for request ID
        
    Returns:
        HTTPException: Formatted exception
    """
    request_id = None
    if request:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    error_response = ErrorResponse(
        detail=detail,
        type=error_type,
        timestamp=datetime.now(timezone.utc),
        request_id=request_id
    )
    
    return HTTPException(
        status_code=status_code,
        detail=error_response.model_dump()
    )


def create_validation_error(
    detail: str,
    field: Optional[str] = None,
    request: Optional[Request] = None
) -> HTTPException:
    """Create a 400 validation error.
    
    Args:
        detail: Validation error message
        field: Optional field name that failed validation
        request: Optional request object
        
    Returns:
        HTTPException: 400 validation error
    """
    # Don't add field prefix for cleaner error messages
    return create_http_exception(
        status_code=400,
        detail=detail,
        error_type="validation_error",
        request=request
    )


def create_not_found_error(
    resource: str,
    identifier: Optional[str] = None,
    request: Optional[Request] = None
) -> HTTPException:
    """Create a 404 not found error.
    
    Args:
        resource: Type of resource not found
        identifier: Optional resource identifier
        request: Optional request object
        
    Returns:
        HTTPException: 404 not found error
    """
    if identifier:
        detail = f"{resource} with identifier '{identifier}' not found"
    else:
        detail = f"{resource} not found"
    
    return create_http_exception(
        status_code=404,
        detail=detail,
        error_type="not_found_error",
        request=request
    )


def create_conflict_error(
    detail: str,
    request: Optional[Request] = None
) -> HTTPException:
    """Create a 409 conflict error.
    
    Args:
        detail: Conflict error message
        request: Optional request object
        
    Returns:
        HTTPException: 409 conflict error
    """
    return create_http_exception(
        status_code=409,
        detail=detail,
        error_type="conflict_error",
        request=request
    )


def create_forbidden_error(
    detail: str = "Access forbidden",
    request: Optional[Request] = None
) -> HTTPException:
    """Create a 403 forbidden error.
    
    Args:
        detail: Forbidden error message
        request: Optional request object
        
    Returns:
        HTTPException: 403 forbidden error
    """
    return create_http_exception(
        status_code=403,
        detail=detail,
        error_type="forbidden_error",
        request=request
    )


def create_unauthorized_error(
    detail: str = "Authentication required",
    request: Optional[Request] = None
) -> HTTPException:
    """Create a 401 unauthorized error.
    
    Args:
        detail: Unauthorized error message
        request: Optional request object
        
    Returns:
        HTTPException: 401 unauthorized error
    """
    error = create_http_exception(
        status_code=401,
        detail=detail,
        error_type="unauthorized_error",
        request=request
    )
    error.headers = {"WWW-Authenticate": "Bearer"}
    return error


# Common validation patterns
def validate_discord_id(value: str, field_name: str = "ID") -> str:
    """Validate Discord snowflake ID format.
    
    Args:
        value: ID string to validate
        field_name: Name of the field for error messages
        
    Returns:
        str: Validated ID
        
    Raises:
        HTTPException: If ID format is invalid
    """
    try:
        id_int = int(value)
        if id_int <= 0:
            raise ValueError("ID must be positive")
        return value
    except ValueError:
        raise create_validation_error(
            f"Invalid {field_name} format"
        )


def validate_positive_integer(value: int, field_name: str, min_value: int = 1) -> int:
    """Validate positive integer.
    
    Args:
        value: Integer to validate
        field_name: Name of the field for error messages
        min_value: Minimum allowed value
        
    Returns:
        int: Validated integer
        
    Raises:
        HTTPException: If integer is invalid
    """
    if value < min_value:
        raise create_validation_error(
            f"Value must be at least {min_value}",
            field=field_name
        )
    return value