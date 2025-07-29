"""HTTP methods middleware for standardized method handling.

This module provides middleware to handle HTTP methods consistently
and prevent information disclosure through method-specific error codes.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from smarter_dev.shared.config import get_settings


class HTTPMethodsMiddleware(BaseHTTPMiddleware):
    """Middleware for standardized HTTP method handling.
    
    This middleware provides consistent responses for unsupported HTTP methods
    and prevents information disclosure through method enumeration.
    """
    
    def __init__(self, app: ASGIApp) -> None:
        """Initialize HTTP methods middleware.
        
        Args:
            app: ASGI application
        """
        super().__init__(app)
        
        # Standard HTTP methods that should be handled consistently
        self.allowed_methods = {
            "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
        }
        
        # Methods that should always be rejected
        self.rejected_methods = {
            "TRACE", "CONNECT"  # Security sensitive methods
        }
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Handle HTTP methods consistently.
        
        Args:
            request: HTTP request
            call_next: Next middleware/endpoint
            
        Returns:
            Response: HTTP response with standardized method handling
        """
        method = request.method.upper()
        
        # Always reject dangerous methods
        if method in self.rejected_methods:
            return self._create_method_not_allowed_response()
        
        # Process the request normally
        response = await call_next(request)
        
        # If response is 405 (Method Not Allowed), standardize the response
        if response.status_code == 405:
            return self._create_method_not_allowed_response()
        
        return response
    
    def _create_method_not_allowed_response(self) -> JSONResponse:
        """Create standardized method not allowed response.
        
        Returns:
            JSONResponse: Standardized 405 response
        """
        return JSONResponse(
            status_code=405,
            content={"detail": "Method not allowed"},
            headers={
                "Allow": "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
            }
        )


class StarletteHTTPMethodsMiddleware:
    """Starlette-compatible HTTP methods middleware.
    
    This class provides the same HTTP method handling functionality but
    using Starlette's ASGI middleware pattern.
    """
    
    def __init__(self, app: ASGIApp) -> None:
        """Initialize Starlette HTTP methods middleware.
        
        Args:
            app: ASGI application
        """
        self.app = app
        self.rejected_methods = {"TRACE", "CONNECT"}
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI callable for Starlette middleware.
        
        Args:
            scope: ASGI scope
            receive: ASGI receive callable
            send: ASGI send callable
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        method = scope.get("method", "").upper()
        
        # Always reject dangerous methods
        if method in self.rejected_methods:
            await self._send_method_not_allowed(send)
            return
        
        # Track if we need to modify 405 responses
        response_started = False
        
        async def send_with_method_handling(message):
            nonlocal response_started
            
            if (message["type"] == "http.response.start" and 
                message.get("status") == 405 and not response_started):
                # Standardize 405 responses
                response_started = True
                await self._send_method_not_allowed(send)
                return
            elif message["type"] == "http.response.body" and response_started:
                # Skip sending body if we already sent our custom response
                return
            
            await send(message)
        
        await self.app(scope, receive, send_with_method_handling)
    
    async def _send_method_not_allowed(self, send: Send) -> None:
        """Send standardized method not allowed response.
        
        Args:
            send: ASGI send callable
        """
        response_body = b'{"detail":"Method not allowed"}'
        
        await send({
            "type": "http.response.start",
            "status": 405,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(response_body)).encode()],
                [b"allow", b"GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"],
            ],
        })
        
        await send({
            "type": "http.response.body",
            "body": response_body,
        })


def create_http_methods_middleware(starlette_compatible: bool = False):
    """Factory function to create HTTP methods middleware.
    
    Args:
        starlette_compatible: Whether to use Starlette-compatible version
        
    Returns:
        Configured middleware class or callable
    """
    if starlette_compatible:
        return StarletteHTTPMethodsMiddleware
    else:
        return HTTPMethodsMiddleware