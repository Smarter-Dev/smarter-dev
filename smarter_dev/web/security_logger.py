"""Security events for the bot API: failed authentication, rate limit exceeded
and admin operations.

Events are structured logs, not database rows (#81): sent to Pydantic Logfire
when it is configured (``LOGFIRE_TOKEN``, see
:mod:`smarter_dev.shared.observability`), and to the standard logger
otherwise. Each carries the event name, outcome, the API key involved, the
route template and the method. The client IP is recorded only for failed
authentication, where it identifies the source of the attempt; the other
events come from the bot's own authenticated key.

Event names, attribute names and fixed values avoid the words Logfire's
default scrubber redacts (``auth``, ``api key``, ``session``, ``secret`` and
the like), so events arrive readable: ``login_failed`` rather than
"authentication failed", ``key_id`` rather than "api key id". Each event
method's values are fixed codes for the same reason.

No event records a member's Discord id. Paths are recorded as route
templates (``/api/guilds/{guild_id}/bytes/balance/{user_id}``), never the
concrete path, and ordinary successful requests are not logged at all: rate
limiting counts them in Redis.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Protocol
from uuid import UUID

import logfire
from litestar import Request
from skrift.lib.client_ip import get_client_ip

from smarter_dev.shared.observability import logfire_enabled

logger = logging.getLogger(__name__)


class AuthenticatedKeyLike(Protocol):
    """The slice of an authenticated API key the logger consumes.

    Satisfied by ``api_native.rate_limiting.RateLimitedKey`` (built from the
    Skrift-native key).
    """

    id: UUID
    key_prefix: str
    created_by: str


def route_template(request: Request) -> str | None:
    """The matched route's template, e.g. ``/api/guilds/{guild_id}/bytes/config``.

    Recorded instead of the concrete path, which carries the Discord ids of
    the members a request is about. None when no route matched.
    """
    return request.scope.get("path_template") or None


class SecurityLogger:
    """Emits security events to Logfire, or the standard logger without it."""

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.SecurityLogger")

    def emit(self, event: str, success: bool, **attributes: Any) -> None:
        """Emit one event. Never raises: logging must not break a request."""
        attributes = {"security.event": event, "success": success, **attributes}
        if logfire_enabled():
            try:
                logfire.log(
                    "info" if success else "warn",
                    "Security event: {security.event}",
                    attributes=attributes,
                    tags=["security"],
                )
                return
            except Exception:
                self.logger.debug("Could not emit security event to Logfire", exc_info=True)
        self.logger.log(
            logging.INFO if success else logging.WARNING,
            "Security event: %s %s",
            event,
            attributes,
            extra={"security_event": attributes},
        )

    async def log_authentication_failed(
        self,
        bearer_presented: bool,
        request: Request,
        reason: str,
    ) -> None:
        """Log a failed authentication attempt as ``login_failed``.

        Records only whether a bearer was presented, never any part of it: a
        rejected token may be a real key sent to the wrong place, and a prefix
        of it is a credential fragment. ``reason`` is a code
        (``no_valid_key``, ``insufficient_permissions``). The client IP is the
        one the limiter and Skrift use, resolved through trusted proxies.
        """
        self.emit(
            "login_failed",
            False,
            bearer_presented=bearer_presented,
            reason=reason,
            route=route_template(request),
            method=request.method,
            client_ip=get_client_ip(request.scope),
        )

    async def log_rate_limit_exceeded(
        self,
        api_key: AuthenticatedKeyLike,
        request: Request,
        current_usage: int,
        limit: int,
        window: str,
    ) -> None:
        """Log a rate limit violation."""
        self.emit(
            "rate_limit_exceeded",
            False,
            key_id=str(api_key.id),
            key_prefix=api_key.key_prefix,
            current_usage=current_usage,
            rate_limit=limit,
            window=window,
            route=route_template(request),
            method=request.method,
        )

    async def log_admin_operation(
        self,
        operation: str,
        user_identifier: str,
        request: Request,
        success: bool = True,
        details: str | None = None,
    ) -> None:
        """Log an administrative operation.

        ``details`` must not name a member; callers describe the operation's
        scope (a guild, a page, a conversation id) and nothing more.
        """
        self.emit(
            "admin_operation",
            success,
            operation=operation,
            caller=user_identifier,
            details=details or f"Admin operation: {operation}",
            route=route_template(request),
            method=request.method,
        )


# Global security logger instance
security_logger = SecurityLogger()


def get_security_logger() -> SecurityLogger:
    """Get the global security logger instance."""
    return security_logger
