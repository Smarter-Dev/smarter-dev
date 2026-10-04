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
method's values are fixed codes for the same reason. Values we do not
choose go under keys Logfire never scrubs: the route template and method as
``http.route`` and ``http.method`` (``/api/auth/validate`` would otherwise be
redacted for containing "auth"). The calling key is identified by its id
alone: not by its free-text name, nor by its random prefix, either of which
can contain a scrubbed word.

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
    Skrift-native key) and by the Skrift key row itself.
    """

    id: UUID


def _http(request: Request) -> dict[str, Any]:
    """Route template and method, under OpenTelemetry's names. Logfire treats
    both keys as safe, so a template like ``/api/auth/status`` arrives intact."""
    return {"http.route": route_template(request), "http.method": request.method}


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
            client_ip=get_client_ip(request.scope),
            **_http(request),
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
            current_usage=current_usage,
            rate_limit=limit,
            window=window,
            **_http(request),
        )

    async def log_admin_operation(
        self,
        operation: str,
        api_key: AuthenticatedKeyLike,
        request: Request,
        success: bool = True,
        details: str | None = None,
    ) -> None:
        """Log an administrative operation by the calling API key.

        The key is identified by id alone; its display name and prefix can
        contain words Logfire's scrubber redacts. ``details`` must not name a
        member; callers describe the operation's scope (a guild, a page, a
        conversation id) and nothing more.
        """
        self.emit(
            "admin_operation",
            success,
            operation=operation,
            key_id=str(api_key.id),
            details=details or f"Admin operation: {operation}",
            **_http(request),
        )


# Global security logger instance
security_logger = SecurityLogger()


def get_security_logger() -> SecurityLogger:
    """Get the global security logger instance."""
    return security_logger
