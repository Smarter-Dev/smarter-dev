"""Security headers middleware for enhanced web application security.

This module provides middleware to add essential security headers that protect
against common web vulnerabilities including clickjacking, XSS, MIME sniffing,
and more.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from smarter_dev.shared.config import get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware that adds security headers to all HTTP responses.
    
    This middleware adds the following security headers:
    - X-Frame-Options: Prevents clickjacking attacks
    - X-Content-Type-Options: Prevents MIME type sniffing
    - X-XSS-Protection: Enables XSS filtering (legacy browsers)
    - Strict-Transport-Security: Enforces HTTPS connections
    - Content-Security-Policy: Prevents various injection attacks
    - Referrer-Policy: Controls referrer information sharing
    - Permissions-Policy: Controls browser feature access
    """
    
    def __init__(
        self,
        app: ASGIApp,
        enable_hsts: bool = True,
        hsts_max_age: int = 31536000,  # 1 year
        include_subdomains: bool = True,
        enable_csp: bool = True,
        csp_policy: str | None = None
    ) -> None:
        """Initialize security headers middleware.
        
        Args:
            app: ASGI application
            enable_hsts: Whether to enable HSTS header
            hsts_max_age: HSTS max age in seconds
            include_subdomains: Whether to include subdomains in HSTS
            enable_csp: Whether to enable Content Security Policy
            csp_policy: Custom CSP policy string
        """
        super().__init__(app)
        self.enable_hsts = enable_hsts
        self.hsts_max_age = hsts_max_age
        self.include_subdomains = include_subdomains
        self.enable_csp = enable_csp
        self.csp_policy = csp_policy or self._default_csp_policy()
        
    def _default_csp_policy(self) -> str:
        """Generate default Content Security Policy.
        
        Returns:
            str: Default CSP policy string
        """
        return (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Add security headers to response.
        
        Args:
            request: HTTP request
            call_next: Next middleware/endpoint
            
        Returns:
            Response: HTTP response with security headers
        """
        response = await call_next(request)
        
        # Add security headers
        response.headers.update(self._get_security_headers(request))
        
        return response
    
    def _get_security_headers(self, request: Request) -> dict[str, str]:
        """Get security headers dictionary.
        
        Args:
            request: HTTP request for context
            
        Returns:
            dict: Security headers to add
        """
        headers = {
            # Prevent clickjacking by denying framing
            "X-Frame-Options": "DENY",
            
            # Prevent MIME type sniffing
            "X-Content-Type-Options": "nosniff",
            
            # Enable XSS protection (legacy browsers)
            "X-XSS-Protection": "1; mode=block",
            
            # Control referrer information
            "Referrer-Policy": "strict-origin-when-cross-origin",
            
            # Restrict browser features
            "Permissions-Policy": (
                "geolocation=(), "
                "microphone=(), "
                "camera=(), "
                "payment=(), "
                "usb=(), "
                "magnetometer=(), "
                "gyroscope=(), "
                "accelerometer=()"
            ),
            
            # Prevent caching of sensitive pages
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        
        # Add HSTS header for HTTPS connections
        if self.enable_hsts and self._is_secure_request(request):
            hsts_value = f"max-age={self.hsts_max_age}"
            if self.include_subdomains:
                hsts_value += "; includeSubDomains"
            headers["Strict-Transport-Security"] = hsts_value
        
        # Add Content Security Policy
        if self.enable_csp:
            headers["Content-Security-Policy"] = self.csp_policy
        
        return headers
    
    def _is_secure_request(self, request: Request) -> bool:
        """Check if request is over HTTPS.
        
        Args:
            request: HTTP request
            
        Returns:
            bool: True if request is secure
        """
        # Check direct HTTPS
        if request.url.scheme == "https":
            return True
            
        # Check forwarded protocol headers (for reverse proxies)
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
        if forwarded_proto == "https":
            return True
            
        # Check if running in production (assume HTTPS)
        settings = get_settings()
        return settings.is_production


class StarletteFriendlySecurityMiddleware:
    """Starlette-compatible security headers middleware.
    
    This class provides the same security headers functionality but
    using Starlette's ASGI middleware pattern instead of FastAPI's
    BaseHTTPMiddleware pattern.
    """
    
    def __init__(
        self,
        app: ASGIApp,
        enable_hsts: bool = True,
        hsts_max_age: int = 31536000,
        include_subdomains: bool = True,
        enable_csp: bool = True,
        csp_policy: str | None = None
    ) -> None:
        """Initialize Starlette security middleware.
        
        Args:
            app: ASGI application
            enable_hsts: Whether to enable HSTS header
            hsts_max_age: HSTS max age in seconds
            include_subdomains: Whether to include subdomains in HSTS
            enable_csp: Whether to enable Content Security Policy
            csp_policy: Custom CSP policy string
        """
        self.app = app
        self.enable_hsts = enable_hsts
        self.hsts_max_age = hsts_max_age
        self.include_subdomains = include_subdomains
        self.enable_csp = enable_csp
        self.csp_policy = csp_policy or self._default_csp_policy()
    
    def _default_csp_policy(self) -> str:
        """Generate default Content Security Policy.
        
        Returns:
            str: Default CSP policy string
        """
        return (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
    
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
        
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                
                # Add security headers
                security_headers = self._get_security_headers(scope)
                for name, value in security_headers.items():
                    headers[name.encode().lower()] = value.encode()
                
                message["headers"] = list(headers.items())
            
            await send(message)
        
        await self.app(scope, receive, send_with_headers)
    
    def _get_security_headers(self, scope: Scope) -> dict[str, str]:
        """Get security headers dictionary.
        
        Args:
            scope: ASGI scope for context
            
        Returns:
            dict: Security headers to add
        """
        headers = {
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "X-XSS-Protection": "1; mode=block",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": (
                "geolocation=(), "
                "microphone=(), "
                "camera=(), "
                "payment=(), "
                "usb=(), "
                "magnetometer=(), "
                "gyroscope=(), "
                "accelerometer=()"
            ),
        }
        
        # Add HSTS header for HTTPS connections
        if self.enable_hsts and self._is_secure_scope(scope):
            hsts_value = f"max-age={self.hsts_max_age}"
            if self.include_subdomains:
                hsts_value += "; includeSubDomains"
            headers["Strict-Transport-Security"] = hsts_value
        
        # Add Content Security Policy
        if self.enable_csp:
            headers["Content-Security-Policy"] = self.csp_policy
        
        return headers
    
    def _is_secure_scope(self, scope: Scope) -> bool:
        """Check if request scope represents HTTPS.
        
        Args:
            scope: ASGI scope
            
        Returns:
            bool: True if request is secure
        """
        # Check direct HTTPS
        if scope.get("scheme") == "https":
            return True
            
        # Check forwarded protocol headers
        headers = dict(scope.get("headers", []))
        forwarded_proto = headers.get(b"x-forwarded-proto", b"").decode().lower()
        if forwarded_proto == "https":
            return True
            
        # Check if running in production
        settings = get_settings()
        return settings.is_production


def create_security_headers_middleware(
    enable_hsts: bool = True,
    hsts_max_age: int = 31536000,
    include_subdomains: bool = True,
    enable_csp: bool = True,
    csp_policy: str | None = None,
    starlette_compatible: bool = False
) -> type[SecurityHeadersMiddleware] | type[StarletteFriendlySecurityMiddleware]:
    """Factory function to create security headers middleware.
    
    Args:
        enable_hsts: Whether to enable HSTS header
        hsts_max_age: HSTS max age in seconds
        include_subdomains: Whether to include subdomains in HSTS
        enable_csp: Whether to enable Content Security Policy
        csp_policy: Custom CSP policy string
        starlette_compatible: Whether to use Starlette-compatible version
        
    Returns:
        Configured middleware class
    """
    settings = get_settings()
    
    # Adjust settings for production
    if settings.is_production:
        enable_hsts = True
        hsts_max_age = 63072000  # 2 years for production
        include_subdomains = True
    
    if starlette_compatible:
        return lambda app: StarletteFriendlySecurityMiddleware(
            app=app,
            enable_hsts=enable_hsts,
            hsts_max_age=hsts_max_age,
            include_subdomains=include_subdomains,
            enable_csp=enable_csp,
            csp_policy=csp_policy
        )
    else:
        return lambda app: SecurityHeadersMiddleware(
            app=app,
            enable_hsts=enable_hsts,
            hsts_max_age=hsts_max_age,
            include_subdomains=include_subdomains,
            enable_csp=enable_csp,
            csp_policy=csp_policy
        )