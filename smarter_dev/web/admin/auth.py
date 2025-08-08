"""Authentication system for admin interface."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable, Any

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from smarter_dev.shared.config import get_settings
from smarter_dev.web.admin.discord_oauth import (
    get_discord_oauth_service,
    DiscordOAuthError,
    InsufficientPermissionsError
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")


class AdminAuthError(Exception):
    """Exception raised for admin authentication errors."""
    pass


def admin_required(func: Callable) -> Callable:
    """Decorator to require admin authentication for view functions.
    
    Args:
        func: The view function to protect
        
    Returns:
        Wrapped function that checks authentication
    """
    @wraps(func)
    async def wrapper(request: Request) -> Any:
        # Check if user is authenticated
        if not request.session.get("is_admin"):
            # Store the requested path for redirect after login (relative URL only)
            next_path = str(request.url.path)
            login_url = f"/admin/login?next={next_path}"
            return RedirectResponse(url=login_url, status_code=303)
        
        return await func(request)
    return wrapper


async def login(request: Request):
    """Admin login handler - Discord OAuth only.
    
    GET: Redirect to Discord OAuth
    POST: Not supported (Discord OAuth only)
    """
    if request.method == "GET":
        # Check if already logged in
        if request.session.get("is_admin"):
            next_url = request.query_params.get("next", "/admin")
            # Ensure next_url is a relative path to prevent open redirects
            if not next_url.startswith("/"):
                next_url = "/admin"
            return RedirectResponse(url=next_url, status_code=303)
        
        # Redirect to Discord OAuth
        try:
            oauth_service = get_discord_oauth_service()
            auth_url = oauth_service.get_authorization_url(request)
            return RedirectResponse(url=auth_url, status_code=303)
        except DiscordOAuthError as e:
            logger.error(f"Discord OAuth configuration error: {e}")
            return templates.TemplateResponse(
                request,
                "admin/login.html",
                {
                    "error": f"Authentication service unavailable: {e}",
                    "config_error": True
                },
                status_code=500
            )
    
    # POST method not supported - Discord OAuth only
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {
            "error": "Username/password authentication is disabled. Please use Discord OAuth.",
            "oauth_only": True
        },
        status_code=405
    )


async def discord_oauth_callback(request: Request):
    """Handle Discord OAuth callback."""
    try:
        oauth_service = get_discord_oauth_service()
        discord_user = await oauth_service.handle_callback(request)
        
        # Set session for authenticated user
        request.session["user_id"] = discord_user.id
        request.session["is_admin"] = True
        request.session["username"] = discord_user.display_name
        request.session["discord_id"] = discord_user.id
        request.session["discord_avatar"] = discord_user.avatar_url
        request.session["auth_method"] = "discord_oauth"
        
        logger.info(f"Discord OAuth login successful for user: {discord_user.display_name}")
        
        # Redirect to originally requested page
        next_url = request.session.pop("oauth_next", "/admin")
        # Ensure next_url is a relative path to prevent open redirects
        if not next_url.startswith("/"):
            next_url = "/admin"
        
        return RedirectResponse(url=next_url, status_code=303)
        
    except InsufficientPermissionsError as e:
        logger.warning(f"Discord OAuth access denied: {e}")
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "permission_error": True
            },
            status_code=403
        )
    except DiscordOAuthError as e:
        logger.error(f"Discord OAuth error: {e}")
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "error": f"Authentication failed: {e}",
                "fallback_login": True
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Unexpected error during Discord OAuth callback: {e}")
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "error": "An unexpected error occurred during authentication. Please try again.",
                "fallback_login": True
            },
            status_code=500
        )


async def logout(request: Request):
    """Admin logout handler."""
    try:
        username = request.session.get("username", "unknown")
        auth_method = request.session.get("auth_method", "unknown")
        request.session.clear()
        
        logger.info(f"Admin logout successful for user: {username} (auth: {auth_method})")
        
        return RedirectResponse(url="/", status_code=303)
    
    except Exception as e:
        logger.error(f"Error during admin logout: {e}")
        # Clear session anyway
        request.session.clear()
        return RedirectResponse(url="/", status_code=303)


def get_current_admin(request: Request) -> str | None:
    """Get the current admin username from session.
    
    Args:
        request: The Starlette request object
        
    Returns:
        Admin username if authenticated, None otherwise
    """
    if request.session.get("is_admin"):
        return request.session.get("username")
    return None


def is_admin_authenticated(request: Request) -> bool:
    """Check if the current request is from an authenticated admin.
    
    Args:
        request: The Starlette request object
        
    Returns:
        True if admin is authenticated, False otherwise
    """
    return bool(request.session.get("is_admin"))