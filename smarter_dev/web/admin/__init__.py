"""Admin interface package for Smarter Dev."""

from __future__ import annotations

__version__ = "1.0.0"

# Admin interface components
from .auth import admin_required, login, logout
from .discord import get_discord_client, get_bot_guilds, get_guild_info, get_guild_roles
from .views import dashboard, guild_list, guild_detail, bytes_config, squads_config
from .routes import admin_routes

__all__ = [
    "admin_required",
    "login", 
    "logout",
    "get_discord_client",
    "get_bot_guilds",
    "get_guild_info", 
    "get_guild_roles",
    "dashboard",
    "guild_list",
    "guild_detail",
    "bytes_config",
    "squads_config",
    "admin_routes"
]