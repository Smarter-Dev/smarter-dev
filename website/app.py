import uvicorn
import os
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.errors import ServerErrorMiddleware

from .routes import home, subscribe
from .admin_routes import (
    admin_login, admin_logout, admin_dashboard, admin_redirects,
    admin_new_redirect, admin_edit_redirect, admin_delete_redirect,
    admin_redirect_detail, admin_analytics, admin_bot_analytics, admin_error_detail, init_admin
)
from .discord_admin_routes import (
    admin_discord_dashboard, admin_discord_users, admin_discord_user_detail,
    admin_discord_warnings, admin_discord_moderation,
    admin_discord_api_keys, admin_discord_api_key_create, admin_discord_api_key_delete,
    admin_discord_bytes, admin_discord_bytes_config, admin_discord_bytes_roles,
    admin_discord_give_bytes, admin_discord_automod
)
from .api_routes import (
    api_token, guild_list, guild_detail, guild_create, guild_update,
    user_list, user_detail, user_create, user_update, users_batch_create,
    bytes_list, bytes_detail, bytes_create, bytes_config_get, bytes_config_create, bytes_config_update,
    bytes_roles_list, bytes_role_create, bytes_role_update, bytes_role_delete,
    bytes_cooldown_get, user_bytes_balance, bytes_leaderboard,
    warning_list, warning_detail, warning_create,
    moderation_case_list, moderation_case_detail, moderation_case_create, moderation_case_update,
    api_key_list, api_key_create, api_key_delete,
    automod_regex_rules_list, automod_regex_rule_detail,
    automod_rate_limits_list, automod_rate_limit_detail
)
from .redirect_handler import handle_redirect
from .auth import AdminAuthBackend, AdminAuthMiddleware
from .api_auth import APIAuthBackend, api_auth_middleware
from .database import engine, get_db
from .models import Base
from .tracking import track_middleware, track_page_view

# Create database tables
Base.metadata.create_all(bind=engine)

# Define routes
routes = [
    Route("/", home, methods=["GET"]),

    # API routes
    Route("/api/auth/token", api_token, methods=["POST"]),
    Route("/api/guilds", guild_list, methods=["GET"]),
    Route("/api/guilds/{guild_id:int}", guild_detail, methods=["GET"]),
    Route("/api/guilds", guild_create, methods=["POST"]),
    Route("/api/guilds/{guild_id:int}", guild_update, methods=["PUT"]),
    Route("/api/users", user_list, methods=["GET"]),
    Route("/api/users/{user_id:int}", user_detail, methods=["GET"]),
    Route("/api/users", user_create, methods=["POST"]),
    Route("/api/users/{user_id:int}", user_update, methods=["PUT"]),
    Route("/api/users/batch", users_batch_create, methods=["POST"]),

    # Bytes routes
    Route("/api/bytes", bytes_list, methods=["GET"]),
    Route("/api/bytes/{bytes_id:int}", bytes_detail, methods=["GET"]),
    Route("/api/bytes", bytes_create, methods=["POST"]),
    Route("/api/bytes/config/{guild_id:int}", bytes_config_get, methods=["GET"]),
    Route("/api/bytes/config", bytes_config_create, methods=["POST"]),
    Route("/api/bytes/config/{guild_id:int}", bytes_config_update, methods=["PUT"]),
    Route("/api/bytes/roles/{guild_id:int}", bytes_roles_list, methods=["GET"]),
    Route("/api/bytes/roles", bytes_role_create, methods=["POST"]),
    Route("/api/bytes/roles/{role_id:int}", bytes_role_update, methods=["PUT"]),
    Route("/api/bytes/roles/{role_id:int}", bytes_role_delete, methods=["DELETE"]),
    Route("/api/bytes/cooldown/{user_id:int}/{guild_id:int}", bytes_cooldown_get, methods=["GET"]),
    Route("/api/bytes/balance/{user_id:int}", user_bytes_balance, methods=["GET"]),
    Route("/api/bytes/leaderboard/{guild_id:int}", bytes_leaderboard, methods=["GET"]),
    Route("/api/warnings", warning_list, methods=["GET"]),
    Route("/api/warnings/{warning_id:int}", warning_detail, methods=["GET"]),
    Route("/api/warnings", warning_create, methods=["POST"]),
    Route("/api/moderation-cases", moderation_case_list, methods=["GET"]),
    Route("/api/moderation-cases/{case_id:int}", moderation_case_detail, methods=["GET"]),
    Route("/api/moderation-cases", moderation_case_create, methods=["POST"]),
    Route("/api/moderation-cases/{case_id:int}", moderation_case_update, methods=["PUT"]),
    Route("/api/automod/regex-rules", automod_regex_rules_list, methods=["GET"]),
    Route("/api/automod/regex-rules/{rule_id:int}", automod_regex_rule_detail, methods=["GET"]),
    Route("/api/automod/rate-limits", automod_rate_limits_list, methods=["GET"]),
    Route("/api/automod/rate-limits/{limit_id:int}", automod_rate_limit_detail, methods=["GET"]),
    Route("/api/subscribe", subscribe, methods=["POST"]),

    # Admin routes
    Route("/admin/login", admin_login, methods=["GET", "POST"]),
    Route("/admin/logout", admin_logout, methods=["GET"]),
    Route("/admin", admin_dashboard, methods=["GET"]),
    Route("/admin/redirects", admin_redirects, methods=["GET"]),
    Route("/admin/redirects/new", admin_new_redirect, methods=["GET", "POST"]),
    Route("/admin/redirects/{id:int}", admin_redirect_detail, methods=["GET"]),
    Route("/admin/redirects/{id:int}/edit", admin_edit_redirect, methods=["GET", "POST"]),
    Route("/admin/redirects/{id:int}/delete", admin_delete_redirect, methods=["POST"]),
    Route("/admin/analytics", admin_analytics, methods=["GET"]),
    Route("/admin/bot-analytics", admin_bot_analytics, methods=["GET"]),
    Route("/admin/errors/{id:int}", admin_error_detail, methods=["GET"]),

    # Discord admin routes
    Route("/admin/discord", admin_discord_dashboard, methods=["GET"]),
    Route("/admin/discord/users", admin_discord_users, methods=["GET"]),
    Route("/admin/discord/users/{id:int}", admin_discord_user_detail, methods=["GET"]),
    Route("/admin/discord/users/{id:int}/give-bytes", admin_discord_give_bytes, methods=["POST"]),
    Route("/admin/discord/warnings", admin_discord_warnings, methods=["GET"]),

    Route("/admin/discord/bytes", admin_discord_bytes, methods=["GET"]),
    Route("/admin/discord/bytes/config", admin_discord_bytes_config, methods=["GET", "POST"]),
    Route("/admin/discord/bytes/roles", admin_discord_bytes_roles, methods=["GET", "POST"]),
    Route("/admin/discord/moderation", admin_discord_moderation, methods=["GET"]),
    Route("/admin/discord/api-keys", admin_discord_api_keys, methods=["GET"]),
    Route("/admin/discord/api-keys/new", admin_discord_api_key_create, methods=["GET", "POST"]),
    Route("/admin/discord/api-keys/{id:int}/delete", admin_discord_api_key_delete, methods=["POST"]),
    Route("/admin/discord/automod", admin_discord_automod, methods=["GET", "POST"]),

    # Static files - must be before the catch-all redirect handler
    Mount("/static", app=StaticFiles(directory="website/static"), name="static"),

    # Redirect handler - must be last to catch all other paths
    Route("/{path:path}", handle_redirect, methods=["GET"]),
]

# Middleware
middleware = [
    Middleware(ServerErrorMiddleware, debug=True),  # Add error middleware to catch exceptions
    Middleware(SessionMiddleware, secret_key="smarter-dev-secret-key"),
    Middleware(AuthenticationMiddleware, backend=AdminAuthBackend()),
    Middleware(AdminAuthMiddleware)
    # We'll add our tracking middleware and API auth middleware after app creation
]

# Create Starlette application
app = Starlette(
    debug=True,
    routes=routes,
    middleware=middleware
)

# Apply tracking middleware
app.add_middleware(track_middleware)

# Create a wrapper for the API authentication middleware
@app.middleware("http")
async def api_auth_middleware_wrapper(request, call_next):
    return await api_auth_middleware(request, call_next)

# Apply tracking decorator to routes
# Note: This is an alternative to using middleware. You can use either approach.
# Uncomment these lines if you prefer using decorators instead of middleware.
#
# # Apply tracking decorator to non-admin routes
# app.routes = [
#     Route(route.path, track_page_view(route.endpoint), methods=route.methods, name=route.name)
#     if not route.path.startswith('/admin') and not isinstance(route, Mount) else route
#     for route in app.routes
# ]

# We're using middleware classes instead of this custom middleware function
# The SessionMiddleware and AuthenticationMiddleware are already in the middleware stack

# Initialize admin user if not exists
@app.on_event("startup")
def startup_event():
    # Get DB session
    db = next(get_db())

    # Create admin user with environment variables or defaults
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@smarter.dev")
    admin_password = os.environ.get("ADMIN_PASSWORD", "smarterdev2024")

    init_admin(db, admin_username, admin_email, admin_password)

# Run the application if executed directly
if __name__ == "__main__":
    uvicorn.run("website.app:app", host="0.0.0.0", port=8000, reload=True)
