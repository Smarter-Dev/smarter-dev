"""Error helpers and exception handlers for the native bot API controllers.

The legacy FastAPI mount produced three distinct error-body shapes that the
bot client (``smarter_dev/bot/services/api_client.py``) and other consumers
depend on. These helpers reproduce them byte-for-byte so the ported Litestar
controllers stay wire-compatible:

- **Nested** ``{"detail": {"detail", "type", "errors", "timestamp",
  "request_id"}}`` — from the FastAPI ``create_validation_error`` /
  ``create_conflict_error`` helpers (an ``HTTPException`` whose ``detail`` is a
  full ``ErrorResponse``). Built here by :func:`validation_error` /
  :func:`conflict_error`.
- **Flat** ``{"detail", "type", "errors", "timestamp", "request_id"}`` — from
  the FastAPI app-level exception handlers for the ``crud`` exception
  hierarchy. Built here by the ``handle_*`` handlers below.
- **Plain** ``{"detail": "<string>"}`` — from a bare FastAPI ``HTTPException``
  (guild-id validation). Built here by :func:`plain_error`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from litestar import Request, Response
from litestar.exceptions import ValidationException

from smarter_dev.shared.config import get_settings
from smarter_dev.web.crud import (
    ConflictError,
    DatabaseOperationError,
    NotFoundError,
)


class BotApiException(Exception):
    """Carrier exception producing a pre-built JSON error body and status.

    Handlers raise this for the FastAPI ``create_*`` / bare ``HTTPException``
    error shapes, which have no natural type in the ``crud`` hierarchy.
    """

    def __init__(self, status_code: int, body: dict) -> None:
        super().__init__(f"{status_code}: {body}")
        self.status_code = status_code
        self.body = body


def _request_id(request: Request) -> str:
    """Mirror the FastAPI request-id middleware: header value or a fresh UUID."""
    return request.headers.get("x-request-id") or str(uuid4())


def _error_response(detail: str, error_type: str, request_id: str) -> dict:
    """Serialize an ``ErrorResponse`` exactly as the FastAPI schema did."""
    return {
        "detail": detail,
        "type": error_type,
        "errors": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
    }


def validation_error(request: Request, detail: str) -> BotApiException:
    """400 with the nested ``create_validation_error`` body shape."""
    return BotApiException(
        400, {"detail": _error_response(detail, "validation_error", _request_id(request))}
    )


def conflict_error(request: Request, detail: str) -> BotApiException:
    """409 with the nested ``create_conflict_error`` body shape."""
    return BotApiException(
        409, {"detail": _error_response(detail, "conflict_error", _request_id(request))}
    )


def plain_error(status_code: int, detail: str) -> BotApiException:
    """Bare ``HTTPException`` body: ``{"detail": "<string>"}``."""
    return BotApiException(status_code, {"detail": detail})


def _verbose_or_generic(verbose_detail: str, generic_detail: str) -> str:
    """Return the verbose detail only in verbose development, else the generic.

    Mirrors ``smarter_dev.web.api.security_utils.create_generic_error_response``:
    detailed messages are exposed solely when ``verbose_errors_enabled`` and
    ``is_development`` are both set.
    """
    settings = get_settings()
    if settings.verbose_errors_enabled and settings.is_development:
        return verbose_detail
    return generic_detail


def secure_validation_error(message: str) -> BotApiException:
    """400 plain body — mirrors ``security_utils.create_validation_error``."""
    return plain_error(400, _verbose_or_generic(message, "Invalid request"))


def secure_not_found_error(resource: str = "Resource") -> BotApiException:
    """404 plain body — mirrors ``security_utils.create_not_found_error``."""
    return plain_error(404, _verbose_or_generic(f"{resource} not found", "Not found"))


def secure_database_error(exc: Exception) -> BotApiException:
    """500 plain body — mirrors ``security_utils.create_database_error``."""
    return plain_error(
        500, _verbose_or_generic(f"Database error: {exc}", "Internal server error")
    )


def validate_discord_id(request: Request, value: str, field_name: str = "ID") -> str:
    """Validate a Discord snowflake, raising the nested 400 the FastAPI API used."""
    try:
        if int(value) <= 0:
            raise ValueError("ID must be positive")
        return value
    except ValueError:
        raise validation_error(request, f"Invalid {field_name} format")


def handle_bot_api_exception(request: Request, exc: BotApiException) -> Response:
    """Render a :class:`BotApiException` as its pre-built body."""
    return Response(content=exc.body, status_code=exc.status_code)


def handle_not_found(request: Request, exc: NotFoundError) -> Response:
    """404 flat body — mirrors the FastAPI ``NotFoundError`` handler."""
    return Response(
        content=_error_response(str(exc), "not_found_error", _request_id(request)),
        status_code=404,
    )


def handle_conflict(request: Request, exc: ConflictError) -> Response:
    """409 flat body — mirrors the FastAPI ``ConflictError`` handler."""
    return Response(
        content=_error_response(str(exc), "conflict_error", _request_id(request)),
        status_code=409,
    )


def handle_database_error(request: Request, exc: DatabaseOperationError) -> Response:
    """500 flat body — mirrors the FastAPI ``DatabaseOperationError`` handler.

    The verbose detail gate matches FastAPI exactly: internal error strings are
    exposed only in development with verbose errors enabled.
    """
    settings = get_settings()
    if settings.verbose_errors_enabled and settings.is_development:
        detail = f"Database error: {exc}"
    else:
        detail = "Internal server error"
    return Response(
        content=_error_response(detail, "database_error", _request_id(request)),
        status_code=500,
    )


def handle_validation_exception(request: Request, exc: ValidationException) -> Response:
    """422 body — mirrors the FastAPI ``RequestValidationError`` handler.

    Litestar raises ``ValidationException`` (HTTP 400 by default) for request
    body / query-parameter validation failures; FastAPI answered those with
    422 and a ``ValidationErrorResponse``. Re-map to 422 with the same shape.
    """
    raw_errors = exc.extra if isinstance(exc.extra, list) else []
    errors = [
        {
            "code": str(item.get("type", "value_error")) if isinstance(item, dict) else "value_error",
            "message": str(item.get("message", item)) if isinstance(item, dict) else str(item),
            "field": str(item.get("key")) if isinstance(item, dict) and item.get("key") else None,
        }
        for item in raw_errors
    ]
    return Response(
        content={
            "detail": "Request validation failed",
            "type": "validation_error",
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": _request_id(request),
        },
        status_code=422,
    )


# Registered on the native controllers (and, at switchover, could move to the
# app). ``crud`` subclasses (NotFoundError, ConflictError) are keyed explicitly
# so Litestar's MRO lookup does not fall through to the DatabaseOperationError
# handler.
BOT_API_EXCEPTION_HANDLERS = {
    BotApiException: handle_bot_api_exception,
    NotFoundError: handle_not_found,
    ConflictError: handle_conflict,
    DatabaseOperationError: handle_database_error,
    ValidationException: handle_validation_exception,
}
