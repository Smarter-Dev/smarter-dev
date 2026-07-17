"""Multi-tier rate limiting for the native bytes endpoints.

Litestar port of the retired FastAPI ``smarter_dev/web/multi_tier_rate_limiter.py``
(docs/v2/legacy-sunset/04-api-rewrite.md, "Rate-limiting parity" option A —
the port lives here because Skrift's built-in rate limit config cannot express
the three windows or the legacy header set). Scope parity: in the FastAPI app
only the bytes router applied ``apply_rate_limiting``, so this middleware is
attached to :class:`~smarter_dev.web.api_native.bytes.BytesController` only.

Behavior kept byte-compatible with the legacy implementation:

- Three windows per API key: 10 req/s, 180 req/min, 2500 req/15 min — the
  fixed values the legacy ``AuthenticatedKey`` applied to Skrift-native keys
  (the ``skrift.api_keys`` table carries no per-window limits).
- Usage counting is DB-backed: ``security_logs`` rows with
  ``action == "api_request"`` for the key's id, and every allowed request
  logs one such row (the counter's own data source).
- Success responses carry ``x-ratelimit-limit/remaining/reset`` plus the
  per-window ``-second`` / ``-minute`` / ``-15min`` variants.
- Exceeding any window answers 429 with the legacy ``{"detail": ...}`` body,
  a ``retry-after`` header escalated to the next tier's duration, and the
  escalated header set (the bot's ``api_client`` reads these to self-throttle).

Requests without a verifiable ``sk_`` bearer pass through untouched — the
route guards answer 401 and, like the legacy flow (auth dependency ran before
rate limiting), unauthenticated traffic never consumes or reports windows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from litestar import Request
from litestar.datastructures import MutableScopeHeaders
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from skrift.db.services import api_key_service as skrift_api_key_service
from skrift.lib.client_ip import get_client_ip
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.web.models import SecurityLog
from smarter_dev.web.security_logger import get_security_logger

logger = logging.getLogger(__name__)

# Rate-limit windows applied to Skrift-native keys — identical to the legacy
# ``dependencies.SKRIFT_KEY_RATE_LIMIT_*`` defaults (strictest first).
RATE_LIMIT_PER_SECOND = 10
RATE_LIMIT_PER_MINUTE = 180
RATE_LIMIT_PER_15_MINUTES = 2500


@dataclass(frozen=True)
class RateLimitWindow:
    """Configuration for a single rate limiting window."""

    name: str
    duration_seconds: int
    limit: int
    header_suffix: str


@dataclass(frozen=True)
class RateLimitedKey:
    """The slice of the authenticated key the limiter and logger consume."""

    id: UUID
    key_prefix: str
    created_by: str


RATE_LIMIT_WINDOWS: tuple[RateLimitWindow, ...] = (
    RateLimitWindow("second", 1, RATE_LIMIT_PER_SECOND, "second"),
    RateLimitWindow("minute", 60, RATE_LIMIT_PER_MINUTE, "minute"),
    RateLimitWindow("15min", 900, RATE_LIMIT_PER_15_MINUTES, "15min"),
)


def rate_limited_key_from_skrift(skrift_api_key) -> RateLimitedKey:
    """Build the limiter's key view — mirrors the legacy skrift-branch shim."""
    principal_name = (
        skrift_api_key.service_name
        or skrift_api_key.display_name
        or str(skrift_api_key.user_id)
    )
    return RateLimitedKey(
        id=skrift_api_key.id,
        key_prefix=skrift_api_key.key_prefix,
        created_by=principal_name,
    )


def _next_tier_window(exceeded: RateLimitWindow) -> RateLimitWindow:
    """Escalate to the next (more lenient) tier, or stay at the highest."""
    exceeded_index = RATE_LIMIT_WINDOWS.index(exceeded)
    if exceeded_index + 1 < len(RATE_LIMIT_WINDOWS):
        return RATE_LIMIT_WINDOWS[exceeded_index + 1]
    return exceeded


async def _usage_count_for_window(
    api_key: RateLimitedKey,
    session: AsyncSession,
    window: RateLimitWindow,
    current_time: datetime,
) -> int:
    """Count ``api_request`` security-log rows for the key within the window."""
    window_start = current_time - timedelta(seconds=window.duration_seconds)
    count_stmt = select(func.count(SecurityLog.id)).where(
        and_(
            SecurityLog.api_key_id == api_key.id,
            SecurityLog.action == "api_request",
            SecurityLog.timestamp >= window_start,
        )
    )
    result = await session.execute(count_stmt)
    return result.scalar() or 0


def _success_headers(
    remaining_by_window: list[tuple[RateLimitWindow, int]],
    current_time: datetime,
) -> dict[str, str]:
    """Headers for an allowed request — legacy ``_add_rate_limit_headers``."""
    headers: dict[str, str] = {}
    for window, remaining in remaining_by_window:
        reset_time = current_time + timedelta(seconds=window.duration_seconds)
        headers[f"x-ratelimit-limit-{window.header_suffix}"] = str(window.limit)
        headers[f"x-ratelimit-remaining-{window.header_suffix}"] = str(remaining)
        headers[f"x-ratelimit-reset-{window.header_suffix}"] = str(
            int(reset_time.timestamp())
        )
    strictest_window, strictest_remaining = remaining_by_window[0]
    strictest_reset = current_time + timedelta(
        seconds=strictest_window.duration_seconds
    )
    headers["x-ratelimit-limit"] = str(strictest_window.limit)
    headers["x-ratelimit-remaining"] = str(strictest_remaining)
    headers["x-ratelimit-reset"] = str(int(strictest_reset.timestamp()))
    return headers


def _escalated_429_headers(
    remaining_by_window: list[tuple[RateLimitWindow, int]],
    current_time: datetime,
    escalated_window: RateLimitWindow,
) -> dict[str, str]:
    """Headers for a blocked request — legacy escalation semantics.

    Per-window headers cover only the windows checked before (and including)
    the exceeded one, exactly like the legacy ``zip(windows, results)`` over
    the partial results list; the legacy ``x-ratelimit-*`` trio reports the
    exceeded limit with the escalated tier's reset time.
    """
    headers: dict[str, str] = {}
    for window, remaining in remaining_by_window:
        reset_time = current_time + timedelta(seconds=window.duration_seconds)
        headers[f"x-ratelimit-limit-{window.header_suffix}"] = str(window.limit)
        headers[f"x-ratelimit-remaining-{window.header_suffix}"] = str(remaining)
        headers[f"x-ratelimit-reset-{window.header_suffix}"] = str(
            int(reset_time.timestamp())
        )
    escalated_reset = current_time + timedelta(
        seconds=escalated_window.duration_seconds
    )
    headers["x-ratelimit-limit"] = str(RATE_LIMIT_WINDOWS[0].limit)
    headers["x-ratelimit-remaining"] = "0"
    headers["x-ratelimit-reset"] = str(int(escalated_reset.timestamp()))
    return headers


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of checking every window for one request."""

    allowed: bool
    headers: dict[str, str]
    status_detail: str | None = None
    retry_after_seconds: int | None = None


async def check_rate_limits(
    api_key: RateLimitedKey,
    session: AsyncSession,
    request: Request,
) -> RateLimitDecision:
    """Check all windows (strictest first) and log the request if allowed."""
    current_time = datetime.now(UTC)
    remaining_by_window: list[tuple[RateLimitWindow, int]] = []

    for window in RATE_LIMIT_WINDOWS:
        usage_count = await _usage_count_for_window(
            api_key, session, window, current_time
        )
        if usage_count >= window.limit:
            remaining_by_window.append((window, 0))
            escalated_window = _next_tier_window(window)
            await get_security_logger().log_rate_limit_exceeded(
                session=session,
                api_key=api_key,
                request=request,
                current_usage=usage_count,
                limit=window.limit,
                window=window.name,
            )
            escalated_name = (
                escalated_window.name if escalated_window != window else window.name
            )
            return RateLimitDecision(
                allowed=False,
                headers={
                    "retry-after": str(escalated_window.duration_seconds),
                    **_escalated_429_headers(
                        remaining_by_window, current_time, escalated_window
                    ),
                },
                status_detail=(
                    f"Rate limit of {window.limit} requests per {window.name} "
                    f"exceeded. Must wait until {escalated_name} window resets."
                ),
                retry_after_seconds=escalated_window.duration_seconds,
            )
        remaining_by_window.append((window, max(0, window.limit - usage_count)))

    await get_security_logger().log_api_request(
        session=session, api_key=api_key, request=request, success=True
    )
    return RateLimitDecision(
        allowed=True,
        headers=_success_headers(remaining_by_window, current_time),
    )


async def _resolve_rate_limited_key(scope: Scope, token: str) -> RateLimitedKey | None:
    """Verify the bearer against the Skrift key table; None when invalid."""
    async with get_skrift_db_session_context() as skrift_session:
        skrift_api_key = await skrift_api_key_service.verify_api_key(
            skrift_session, token, client_ip=get_client_ip(scope)
        )
    if skrift_api_key is None:
        return None
    return rate_limited_key_from_skrift(skrift_api_key)


class MultiTierRateLimitMiddleware:
    """ASGI middleware enforcing the multi-tier windows on wrapped routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request: Request = Request(scope)
        authorization_header = request.headers.get("authorization", "")
        token = ""
        if authorization_header.startswith("Bearer "):
            token = authorization_header[7:].strip()

        if not token.startswith("sk_"):
            # No verifiable key — the route guard produces the 401. Legacy
            # parity: rate limiting only ever ran after successful auth.
            await self.app(scope, receive, send)
            return

        api_key = await _resolve_rate_limited_key(scope, token)
        if api_key is None:
            await self.app(scope, receive, send)
            return

        async with get_db_session_context() as log_session:
            decision = await check_rate_limits(api_key, log_session, request)

        if not decision.allowed:
            await _send_rate_limited_response(send, decision)
            return

        async def send_with_rate_limit_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableScopeHeaders(message)
                for header_name, header_value in decision.headers.items():
                    response_headers[header_name] = header_value
            await send(message)

        await self.app(scope, receive, send_with_rate_limit_headers)


async def _send_rate_limited_response(
    send: Send, decision: RateLimitDecision
) -> None:
    """Emit the legacy 429 response: ``{"detail": ...}`` plus escalation headers."""
    body = json.dumps({"detail": decision.status_detail}).encode()
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    raw_headers.extend(
        (name.encode(), value.encode()) for name, value in decision.headers.items()
    )
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": raw_headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
