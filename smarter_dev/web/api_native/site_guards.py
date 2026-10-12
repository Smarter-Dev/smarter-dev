"""Shared principal boundary for site memory and privacy service routes."""

from litestar import Request
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers import BaseRouteHandler
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.auth import resolve_request_api_key


async def site_api_auth_guard(
    connection: ASGIConnection, route_handler: BaseRouteHandler
) -> None:
    """Require an explicitly scoped site service key, never a user/bot key.

    Skrift checks the owner's effective permissions first. Its APIKeyOnly
    marker alone also accepts user keys, so check the principal separately.
    Explicit scopes prevent an unscoped owner/admin key inheriting access.
    """
    await bot_api_auth_guard(connection, route_handler)
    api_key = await resolve_request_api_key(Request(connection.scope))
    required = {
        guard.permission
        for guard in route_handler.guards or []
        if isinstance(guard, Permission)
    }
    if (
        api_key.principal_type != "service"
        or api_key.service_name != "site"
        or not required.issubset(set(api_key.scoped_permission_list))
    ):
        raise NotAuthorizedException("Site service key required")


# Add the endpoint's Permission("site-memory") or Permission("site-privacy")
# on each route, where Skrift's auth guard reads its requirement markers.
SITE_API_GUARDS = [site_api_auth_guard, APIKeyOnly()]
