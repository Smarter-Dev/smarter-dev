"""Native Litestar port of the auth bot API + unauthenticated health probes.

Ports the legacy FastAPI ``routers/auth.py`` (prefix ``/auth``, app.py:389) and
the app-level ``GET /health`` (app.py:437) — unit U1 in
docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact paths, verbs,
status codes, and response shapes of the FastAPI implementation:

- ``GET  /api/health`` → ``{"status": "healthy", "version": "1.0.0"}``,
  **unauthenticated** (monitoring probe) — kept guard-free.
- ``GET  /api/auth/health`` → ``HealthResponse``, unauthenticated.
- ``POST /api/auth/validate`` → ``TokenResponse`` for a valid key.
- ``GET  /api/auth/status`` → key-introspection dict for a valid key.

Auth parity: the legacy ``verify_api_key`` dependency built an
``AuthenticatedKey`` view of the Skrift key row; ``/validate`` and ``/status``
echoed its attributes. Here the Skrift ``auth_guard`` authenticates the request,
and :func:`resolve_request_api_key` re-reads the key row so the handlers can
echo the same attributes the legacy skrift-key branch produced
(``dependencies.authenticated_key_from_skrift``): ``key_name`` is the key's
``display_name``, ``scopes`` its ``scoped_permission_list``, ``usage_count`` 0,
and ``rate_limit`` the fixed Skrift-key hourly window (the Skrift key table
carries no per-window limits).

Intentional status changes vs. the FastAPI mount (see 04-api-rewrite.md
"401-parity" and the harness expectations): missing/malformed Authorization
headers answer **401** from ``auth_guard`` where ``HTTPBearer(auto_error=True)``
answered 403. Legacy ``sk-`` keys are rejected (Skrift's guard only accepts
``sk_``), matching the harness ``auth-legacy-key-401`` check.

Failed-auth audit parity: :func:`bot_api_auth_guard` wraps Skrift's
``auth_guard`` and records rejected requests in ``security_logs``, replacing
the legacy ``verify_api_key`` hookup (see the guard's docstring for what was
intentionally dropped).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from litestar import Controller, Request, get, post
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers import BaseRouteHandler
from litestar.status_codes import HTTP_200_OK

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard
from skrift.db.services import api_key_service as skrift_api_key_service
from skrift.lib.client_ip import get_client_ip

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.api_native.schemas import HealthResponse, TokenResponse
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    plain_error,
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"


async def bot_api_auth_guard(
    connection: ASGIConnection, route_handler: BaseRouteHandler
) -> None:
    """Skrift ``auth_guard`` plus the legacy failed-auth security log.

    The legacy FastAPI ``verify_api_key`` recorded every failed authentication
    in the ``security_logs`` table via
    ``security_logger.log_authentication_failed``. Skrift's guard rejects
    silently, so this wrapper ports that hookup: on ``NotAuthorizedException``
    it writes the failure row (own short-lived session — never the request's)
    and re-raises unchanged. Success-path per-request usage logging
    (``log_api_key_used``) is intentionally dropped: its only consumer was the
    legacy admin stats over the retired legacy key table, and the rate limiter
    keeps its own ``api_request`` rows for the windows it counts.
    """
    from smarter_dev.web.security_logger import get_security_logger

    try:
        await auth_guard(connection, route_handler)
    except NotAuthorizedException as auth_error:
        authorization_header = connection.headers.get("authorization", "")
        token = ""
        if authorization_header.startswith("Bearer "):
            token = authorization_header[7:].strip()
        try:
            await get_security_logger().log_authentication_failed(
                session=None,  # Separate session for reliability
                failed_key_prefix=token[:10],
                request=Request(connection.scope),
                reason=str(auth_error.detail),
            )
        except Exception as log_error:
            # Never let audit logging mask the 401 itself.
            logging.getLogger(__name__).warning(
                "Failed to log bot API authentication failure: %s", log_error
            )
        raise


# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]

# Rate-limit view reported for Skrift-native keys. The Skrift api_keys table
# carries no per-window limits, so key introspection reports the same fixed
# hourly window the legacy ``dependencies.AuthenticatedKey`` defaulted to.
SKRIFT_KEY_RATE_LIMIT_PER_HOUR = 10000


async def resolve_request_api_key(request: Request):
    """Re-read the Skrift key row that authenticated this request.

    The Skrift ``auth_guard`` verifies the bearer but stores nothing on the
    request, so introspection endpoints (and callers needing the key's
    ``display_name`` for audit trails) re-verify the token to get the row.
    Uses a short-lived main-DB session exactly like the legacy skrift-key
    branch (``dependencies._verify_skrift_api_key``) so the request session
    stays untouched.

    Raises the legacy plain 401 body if the key vanished between the guard and
    the handler (revocation race).
    """
    authorization_header = request.headers.get("authorization", "")
    token = ""
    if authorization_header.startswith("Bearer "):
        token = authorization_header[7:].strip()
    if not token:
        raise plain_error(401, "Authentication failed")

    async with get_db_session_context() as skrift_session:
        api_key = await skrift_api_key_service.verify_api_key(
            skrift_session,
            token,
            client_ip=get_client_ip(request.scope),
        )
    if api_key is None:
        raise plain_error(401, "Authentication failed")
    return api_key


class ApiHealthController(Controller):
    """Unauthenticated top-level health probe (legacy ``GET /api/health``)."""

    path = "/api/health"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(status_code=HTTP_200_OK)
    async def health_check(self) -> dict[str, str]:
        """Health check endpoint for monitoring — deliberately guard-free."""
        return {"status": "healthy", "version": "1.0.0"}


class AuthController(Controller):
    """API-key validation and introspection endpoints."""

    path = "/api/auth"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/validate", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def validate_token(self, request: Request) -> TokenResponse:
        """Validate the presented API key (guard already authenticated it)."""
        api_key = await resolve_request_api_key(request)
        return TokenResponse(valid=True, expires_at=api_key.expires_at)

    @get("/health", status_code=HTTP_200_OK)
    async def auth_health_check(self) -> HealthResponse:
        """Health of the auth service — unauthenticated, like the legacy route."""
        settings = get_settings()
        token_configured = bool(settings.discord_bot_token)
        return HealthResponse(
            status="healthy" if token_configured else "degraded",
            version="1.0.0",
            timestamp=datetime.now(timezone.utc),
            database=True,  # Legacy behavior: static placeholder values
            redis=True,
        )

    @get("/status", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def auth_status(self, request: Request) -> dict[str, Any]:
        """Introspection for the authenticated key — legacy skrift-branch shape."""
        api_key = await resolve_request_api_key(request)
        settings = get_settings()
        return {
            "authenticated": True,
            "key_name": api_key.display_name,
            "key_prefix": api_key.key_prefix,
            "scopes": list(api_key.scoped_permission_list),
            "usage_count": 0,
            "rate_limit": SKRIFT_KEY_RATE_LIMIT_PER_HOUR,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
            "environment": settings.environment,
            "api_version": "1.0.0",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


__all__ = [
    "ApiHealthController",
    "bot_api_auth_guard",
    "AuthController",
    "BOT_API_GUARDS",
    "BotApiException",
    "resolve_request_api_key",
]
