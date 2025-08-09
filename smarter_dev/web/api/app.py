"""FastAPI application setup for the Smarter Dev API.

This module creates and configures the FastAPI application with proper lifespan
management, authentication, and routing setup.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from pydantic import ValidationError

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import init_database, close_database
from smarter_dev.web.api.routers.auth import router as auth_router
from smarter_dev.web.api.routers.bytes import router as bytes_router
from smarter_dev.web.api.routers.squads import router as squads_router
from smarter_dev.web.api.routers.admin import router as admin_router
from smarter_dev.web.api.routers.forum_agents_simple import router as forum_agents_router
from smarter_dev.web.api.schemas import ErrorResponse, ValidationErrorResponse, ErrorDetail
from smarter_dev.web.crud import DatabaseOperationError, NotFoundError, ConflictError
from smarter_dev.web.security_headers import SecurityHeadersMiddleware
from smarter_dev.web.http_methods_middleware import HTTPMethodsMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage FastAPI application lifespan.
    
    This function handles initialization and cleanup of database connections
    and other resources during application startup and shutdown.
    
    Args:
        app: FastAPI application instance
        
    Yields:
        None: Control to the application
    """
    # Startup
    settings = get_settings()
    
    try:
        # Initialize database connection
        await init_database()
        
        # Store settings in app state for access in dependencies
        app.state.settings = settings
        
        yield
        
    finally:
        # Cleanup
        await close_database()


# Get settings for configuration
settings = get_settings()

# Configure documentation URLs based on security settings
# If docs require auth, disable default endpoints to force custom authenticated ones
if settings.api_docs_enabled and settings.api_docs_require_auth:
    docs_url = None  # Disable default docs, use authenticated custom endpoints
    redoc_url = None  # Disable default redoc, use authenticated custom endpoints
    openapi_url = None  # Disable default openapi, use authenticated custom endpoints
elif settings.api_docs_enabled:
    docs_url = "/docs"
    redoc_url = "/redoc"
    openapi_url = "/openapi.json"
else:
    docs_url = None
    redoc_url = None
    openapi_url = None

# Disable docs in production unless explicitly enabled
if settings.is_production and not settings.api_docs_enabled:
    docs_url = None
    redoc_url = None
    openapi_url = None

# Create FastAPI application
api = FastAPI(
    title="Smarter Dev API",
    description="REST API for Discord bot bytes economy and squad management",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url
)

# Request ID and debug middleware
@api.middleware("http")
async def add_request_id_middleware(request: Request, call_next):
    """Add request ID to request state for tracking."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    
    # Debug logging for squad join requests
    if "squad" in str(request.url) and request.method == "POST":
        body = b""
        if hasattr(request, "_body"):
            body = request._body
        else:
            # Try to read body
            try:
                body = await request.body()
            except:
                pass
        
        logger.error(f"DEBUG MIDDLEWARE: POST {request.url}, method={request.method}, body={body[:500]}, headers={dict(request.headers)}")
    
    response = await call_next(request)
    
    # Debug logging for failed squad join responses
    if "squad" in str(request.url) and request.method == "POST" and response.status_code >= 400:
        logger.error(f"DEBUG MIDDLEWARE RESPONSE: status={response.status_code}, response_headers={dict(response.headers)}")
    
    response.headers["x-request-id"] = request_id
    return response


# Add HTTP methods middleware (first to handle method validation)
api.add_middleware(HTTPMethodsMiddleware)

# Add security headers middleware (second for all responses)
api.add_middleware(SecurityHeadersMiddleware)

# Add CORS middleware for development (after security headers)
if settings.is_development:
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# Exception handlers
@api.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors.
    
    Args:
        request: FastAPI request object
        exc: Validation exception
        
    Returns:
        JSONResponse: Formatted validation error response
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    # Convert Pydantic errors to our format
    errors = []
    for error in exc.errors():
        field_path = " -> ".join(str(loc) for loc in error["loc"])
        errors.append(ErrorDetail(
            code=error["type"],
            message=error["msg"],
            field=field_path
        ))
    
    logger.warning(
        "Validation error",
        extra={
            "request_id": request_id,
            "url": str(request.url),
            "method": request.method,
            "errors": [error.model_dump() for error in errors]
        }
    )
    
    response = ValidationErrorResponse(
        detail="Request validation failed",
        errors=errors,
        timestamp=datetime.now(timezone.utc),
        request_id=request_id
    )
    
    return JSONResponse(
        status_code=422,
        content=response.model_dump()
    )


@api.exception_handler(NotFoundError)
async def not_found_exception_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    """Handle NotFoundError exceptions.
    
    Args:
        request: FastAPI request object
        exc: NotFoundError exception
        
    Returns:
        JSONResponse: 404 error response
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    logger.info(
        "Resource not found",
        extra={
            "request_id": request_id,
            "url": str(request.url),
            "method": request.method,
            "error": str(exc)
        }
    )
    
    response = ErrorResponse(
        detail=str(exc),
        type="not_found_error",
        timestamp=datetime.now(timezone.utc),
        request_id=request_id
    )
    
    return JSONResponse(
        status_code=404,
        content=response.model_dump()
    )


@api.exception_handler(ConflictError)
async def conflict_exception_handler(request: Request, exc: ConflictError) -> JSONResponse:
    """Handle ConflictError exceptions.
    
    Args:
        request: FastAPI request object
        exc: ConflictError exception
        
    Returns:
        JSONResponse: 409 error response
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    logger.warning(
        "Conflict error",
        extra={
            "request_id": request_id,
            "url": str(request.url),
            "method": request.method,
            "error": str(exc)
        }
    )
    
    response = ErrorResponse(
        detail=str(exc),
        type="conflict_error",
        timestamp=datetime.now(timezone.utc),
        request_id=request_id
    )
    
    return JSONResponse(
        status_code=409,
        content=response.model_dump()
    )


@api.exception_handler(DatabaseOperationError)
async def database_exception_handler(request: Request, exc: DatabaseOperationError) -> JSONResponse:
    """Handle DatabaseOperationError exceptions.
    
    Args:
        request: FastAPI request object
        exc: DatabaseOperationError exception
        
    Returns:
        JSONResponse: 500 error response
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    logger.error(
        f"Database operation error: {exc}",
        extra={
            "request_id": request_id,
            "url": str(request.url),
            "method": request.method,
            "error": str(exc)
        }
    )
    
    # Don't expose internal database errors in production
    if settings.verbose_errors_enabled and settings.is_development:
        detail = f"Database error: {str(exc)}"
    else:
        detail = "Internal server error"
    
    response = ErrorResponse(
        detail=detail,
        type="database_error",
        timestamp=datetime.now(timezone.utc),
        request_id=request_id
    )
    
    return JSONResponse(
        status_code=500,
        content=response.model_dump()
    )


@api.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions globally.
    
    Args:
        request: FastAPI request object
        exc: Exception that was raised
        
    Returns:
        JSONResponse: Error response
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    # Log the full exception with traceback
    logger.exception(
        "Unhandled exception",
        extra={
            "request_id": request_id,
            "url": str(request.url),
            "method": request.method,
            "exception_type": type(exc).__name__
        }
    )
    
    # Don't expose internal error details in production
    if settings.verbose_errors_enabled and settings.is_development:
        detail = f"Internal server error: {str(exc)}"
    else:
        detail = "Internal server error"
    
    response = ErrorResponse(
        detail=detail,
        type="internal_error",
        timestamp=datetime.now(timezone.utc),
        request_id=request_id
    )
    
    return JSONResponse(
        status_code=500,
        content=response.model_dump()
    )


# Include routers
api.include_router(
    auth_router,
    prefix="/auth",
    tags=["Authentication"]
)

api.include_router(
    bytes_router,
    prefix="/guilds/{guild_id}/bytes",
    tags=["Bytes Economy"]
)

api.include_router(
    squads_router,
    prefix="/guilds/{guild_id}/squads",
    tags=["Squad Management"]
)

api.include_router(
    admin_router,
    tags=["Admin Management"]
)

api.include_router(
    forum_agents_router,
    tags=["Forum Agents"]
)


# Health check endpoint
@api.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint for monitoring.
    
    Returns:
        dict: Health status
    """
    return {"status": "healthy", "version": "1.0.0"}


# Custom documentation endpoints with authentication
if settings.api_docs_enabled and settings.api_docs_require_auth:
    from smarter_dev.web.api.dependencies import verify_api_key
    
    @api.get("/docs", include_in_schema=False)
    async def get_swagger_ui(api_key = Depends(verify_api_key)) -> HTMLResponse:
        """Authenticated Swagger UI documentation.
        
        Requires valid API key authentication to access.
        """
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title=f"{api.title} - Swagger UI",
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )
    
    @api.get("/redoc", include_in_schema=False)
    async def get_redoc(api_key = Depends(verify_api_key)) -> HTMLResponse:
        """Authenticated ReDoc documentation.
        
        Requires valid API key authentication to access.
        """
        return get_redoc_html(
            openapi_url="/api/openapi.json",
            title=f"{api.title} - ReDoc",
        )
    
    @api.get("/openapi.json", include_in_schema=False)
    async def get_openapi_schema(api_key = Depends(verify_api_key)) -> dict:
        """Authenticated OpenAPI schema.
        
        Requires valid API key authentication to access.
        """
        return get_openapi(
            title=api.title,
            version=api.version,
            description=api.description,
            routes=api.routes,
        )