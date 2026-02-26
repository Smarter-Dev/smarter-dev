"""Starlette ASGI application for the legacy bot admin interface."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount

from smarter_dev.shared.config import get_settings
from smarter_dev.web.admin.routes import admin_routes


def create_admin_app() -> Starlette:
    """Create the Starlette admin ASGI app with session middleware."""
    settings = get_settings()
    return Starlette(
        routes=[Mount("", routes=admin_routes)],
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=settings.web_session_secret,
                max_age=86400,
            ),
        ],
    )
