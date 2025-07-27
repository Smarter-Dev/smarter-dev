"""Admin interface routing configuration."""

from __future__ import annotations

from starlette.routing import Route

from smarter_dev.web.admin.auth import login, logout, admin_required
from smarter_dev.web.admin.views import (
    dashboard,
    guild_list,
    guild_detail,
    bytes_config,
    squads_config
)


# Define admin routes
admin_routes = [
    # Authentication routes
    Route("/login", login, methods=["GET", "POST"], name="admin_login"),
    Route("/logout", logout, methods=["POST"], name="admin_logout"),
    
    # Dashboard and overview
    Route("/", admin_required(dashboard), name="admin_dashboard"),
    
    # Guild management
    Route("/guilds", admin_required(guild_list), name="admin_guilds"),
    Route("/guilds/{guild_id}", admin_required(guild_detail), name="admin_guild_detail"),
    Route("/guilds/{guild_id}/bytes", admin_required(bytes_config), methods=["GET", "POST"], name="admin_bytes_config"),
    Route("/guilds/{guild_id}/squads", admin_required(squads_config), methods=["GET", "POST"], name="admin_squads_config"),
]