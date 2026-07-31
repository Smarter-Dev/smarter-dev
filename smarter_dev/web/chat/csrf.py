"""CSRF guard for cookie-authenticated Chat API mutations."""

from __future__ import annotations

import hmac

from litestar import Request
from litestar.exceptions import HTTPException
from skrift.forms.core import CSRF_SESSION_KEY


def require_api_csrf(request: Request) -> None:
    stored = str(request.session.get(CSRF_SESSION_KEY, "")) if request.session else ""
    supplied = str(request.headers.get("X-CSRF-Token", ""))
    if not stored or not supplied or not hmac.compare_digest(stored, supplied):
        raise HTTPException(status_code=403, detail="Invalid or expired CSRF token.")
