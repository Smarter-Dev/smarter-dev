"""Project-specific HTTP exception handling."""

from __future__ import annotations

from urllib.parse import urlencode

from litestar import Request
from litestar import Response
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from skrift.app_factory import EXCEPTION_HANDLERS
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.lib.exceptions import (
    http_exception_handler as skrift_http_exception_handler,
)


def _is_admin_page(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _is_authenticated(request: Request) -> bool:
    session = request.scope.get("session")
    return bool(session and session.get(SESSION_USER_ID))


def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Send unauthenticated admin-page visitors through login and back."""
    accepts_html = "text/html" in request.headers.get("accept", "")
    if (
        exc.status_code == 401
        and accepts_html
        and _is_admin_page(request.url.path)
        and not _is_authenticated(request)
    ):
        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        login_url = f"/auth/login?{urlencode({'next': next_url})}"
        return Redirect(path=login_url, status_code=303)

    response = skrift_http_exception_handler(request, exc)
    # Skrift's JSON body carries only the status and detail. A string ``code``
    # in ``extra`` rides along so a client can act on one specific refusal
    # without matching on its wording.
    code = exc.extra.get("code") if isinstance(exc.extra, dict) else None
    if (
        isinstance(code, str)
        and response.media_type == "application/json"
        and isinstance(response.content, dict)
    ):
        response.content = {**response.content, "code": code}
    return response


def install_exception_handlers() -> None:
    """Install Smarter Dev's overrides before Skrift constructs the app."""
    EXCEPTION_HANDLERS[HTTPException] = http_exception_handler
