"""Authentication system for admin interface."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable, Any

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from smarter_dev.shared.config import get_settings

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
            # Store the requested URL for redirect after login
            next_url = str(request.url)
            login_url = f"/admin/login?next={next_url}"
            return RedirectResponse(url=login_url, status_code=303)
        
        return await func(request)
    return wrapper


async def login(request: Request):
    """Admin login page and handler.
    
    GET: Display login form
    POST: Process login credentials
    """
    settings = get_settings()
    
    if request.method == "GET":
        # Check if already logged in
        if request.session.get("is_admin"):
            next_url = request.query_params.get("next", "/admin")
            return RedirectResponse(url=next_url, status_code=303)
        
        return templates.TemplateResponse(
            request,
            "admin/login.html"
        )
    
    # POST - Process login
    try:
        form = await request.form()
        username = form.get("username", "").strip()
        password = form.get("password", "").strip()
        
        # Validate credentials
        if not username or not password:
            raise AdminAuthError("Username and password are required")
        
        # Check credentials
        # In development mode, use simple credentials
        # In production, this would integrate with Discord OAuth
        if settings.is_development:
            if (username == settings.admin_username and 
                password == settings.admin_password):
                
                # Set session
                request.session["user_id"] = username
                request.session["is_admin"] = True
                request.session["username"] = username
                
                logger.info(f"Admin login successful for user: {username}")
                
                # Redirect to requested page or dashboard
                next_url = request.query_params.get("next", "/admin")
                return RedirectResponse(url=next_url, status_code=303)
            else:
                raise AdminAuthError("Invalid username or password")
        else:
            # Production mode would handle Discord OAuth here
            # For now, fall back to development mode
            raise AdminAuthError("Production authentication not yet implemented")
    
    except AdminAuthError as e:
        logger.warning(f"Admin login failed: {e}")
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "error": str(e)
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Unexpected error during admin login: {e}")
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "error": "An unexpected error occurred. Please try again."
            },
            status_code=500
        )


async def logout(request: Request):
    """Admin logout handler."""
    try:
        username = request.session.get("username", "unknown")
        request.session.clear()
        
        logger.info(f"Admin logout successful for user: {username}")
        
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