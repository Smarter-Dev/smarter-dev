"""Tests for project-specific website exception handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from litestar import Response
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from skrift.app_factory import EXCEPTION_HANDLERS
from skrift.auth.session_keys import SESSION_USER_ID

from smarter_dev.web.exception_handlers import http_exception_handler
from smarter_dev.web.exception_handlers import install_exception_handlers


def _request(
    path: str,
    *,
    query: str = "",
    accept: str = "text/html",
) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"accept": accept},
        scope={"session": {}},
        url=SimpleNamespace(path=path, query=query),
    )


def test_admin_401_redirects_to_login_with_full_return_path() -> None:
    response = http_exception_handler(
        _request(
            "/admin/chat-errors/error-id",
            query="source=discord&view=full",
        ),
        NotAuthorizedException(),
    )

    assert response.status_code == 303
    parsed = urlsplit(response.url)
    assert parsed.path == "/auth/login"
    assert parse_qs(parsed.query) == {
        "next": ["/admin/chat-errors/error-id?source=discord&view=full"]
    }


def test_api_401_remains_a_json_unauthorized_response() -> None:
    response = http_exception_handler(
        _request("/api/admin/conversations", accept="application/json"),
        NotAuthorizedException(),
    )

    assert response.status_code == 401
    assert response.media_type == "application/json"


def test_non_html_admin_401_is_not_redirected() -> None:
    response = http_exception_handler(
        _request("/admin/chat-errors/error-id", accept="application/json"),
        NotAuthorizedException(),
    )

    assert response.status_code == 401
    assert response.media_type == "application/json"


def test_authenticated_permission_401_is_not_sent_back_to_login() -> None:
    request = _request("/admin/chat-errors/error-id")
    request.scope["session"][SESSION_USER_ID] = "member-id"
    fallback = Response(
        content="Unauthorized",
        status_code=401,
        media_type="text/html",
    )

    with patch(
        "smarter_dev.web.exception_handlers.skrift_http_exception_handler",
        return_value=fallback,
    ):
        response = http_exception_handler(request, NotAuthorizedException())

    assert response is fallback


def test_install_replaces_skrift_http_exception_handler() -> None:
    original = EXCEPTION_HANDLERS[HTTPException]
    try:
        install_exception_handlers()
        assert EXCEPTION_HANDLERS[HTTPException] is http_exception_handler
    finally:
        EXCEPTION_HANDLERS[HTTPException] = original
