"""Custom page controllers for Smarter Dev.

Handles static marketing pages, redirects, and mounts the FastAPI API.
"""

from litestar import Controller, get
from litestar.handlers import asgi
from litestar.response import Redirect, Template
from litestar.types import Receive, Scope, Send


class PagesController(Controller):
    """Serves marketing pages that don't need database-backed content."""

    path = "/"

    @get("/")
    async def homepage(self) -> Template:
        return Template("index.html")

    @get("/sudo")
    async def sudo_page(self) -> Template:
        return Template("page-sudo.html")

    @get("/discord")
    async def discord_redirect(self) -> Redirect:
        return Redirect("https://discord.gg/de8kajxbYS")


@asgi("/api", is_mount=True, copy_scope=True)
async def api_mount(scope: Scope, receive: Receive, send: Send) -> None:
    """Mount the FastAPI API as an ASGI sub-application."""
    from smarter_dev.web.api.app import api

    # Litestar strips the /api prefix but adds a trailing slash.
    # Normalize the path for FastAPI's routing.
    path: str = scope.get("path", "/")
    if path != "/" and path.endswith("/"):
        scope["path"] = path.rstrip("/")
    await api(scope, receive, send)


@asgi("/bot-admin", is_mount=True, copy_scope=True)
async def bot_admin_mount(scope: Scope, receive: Receive, send: Send) -> None:
    """Mount the legacy Starlette admin interface as an ASGI sub-application."""
    from smarter_dev.web.admin.app import create_admin_app

    path: str = scope.get("path", "/")
    if path != "/" and path.endswith("/"):
        scope["path"] = path.rstrip("/")
    await create_admin_app()(scope, receive, send)
