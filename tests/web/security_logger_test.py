"""Security events go to Logfire when it is on, else the standard logger (#81)."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from smarter_dev.web import security_logger as security_logger_module
from smarter_dev.web.security_logger import SecurityLogger

REPO_ROOT = Path(__file__).resolve().parents[2]


def _request() -> MagicMock:
    request = MagicMock()
    request.method = "GET"
    request.scope = {
        "path_template": "/api/guilds/{guild_id}/bytes/config",
        "state": {"client_ip": "203.0.113.7"},
        "client": ("10.0.0.2", 443),
    }
    return request


def _key() -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-0000000000ab"),
        key_prefix="sk_abcd",
        display_name="Auth Service",  # a name the scrubber would redact
    )


async def _fail_auth() -> None:
    await SecurityLogger().log_authentication_failed(
        bearer_presented=False, request=_request(), reason="no_valid_key"
    )


async def _every_event(security: SecurityLogger) -> None:
    await security.log_authentication_failed(
        bearer_presented=True, request=_request(), reason="no_valid_key"
    )
    await security.log_authentication_failed(
        bearer_presented=True, request=_request(), reason="insufficient_permissions"
    )
    await security.log_rate_limit_exceeded(
        api_key=_key(), request=_request(), current_usage=10, limit=10, window="second"
    )
    await security.log_admin_operation(
        operation="view_help_conversation",
        api_key=_key(),
        request=_request(),
        details="Viewed conversation 00000000-0000-0000-0000-0000000000cd",
    )


def _security_attributes(capfire) -> list[dict]:
    spans = capfire.exporter.exported_spans_as_dict()
    return [
        {
            name: value
            for name, value in span["attributes"].items()
            if not name.startswith("logfire.") and not name.startswith("code.")
        }
        for span in spans
        if "security" in span["attributes"].get("logfire.tags", ())
    ]


@pytest.mark.asyncio
async def test_real_logfire_receives_every_event_unscrubbed(monkeypatch, capfire):
    """Logfire's default scrubber redacts names and values matching ``auth``,
    ``api key`` and the like; none of the events may trip it."""
    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: True)

    await _every_event(SecurityLogger())

    assert _security_attributes(capfire) == [
        {
            "security.event": "login_failed",
            "success": False,
            "bearer_presented": True,
            "reason": "no_valid_key",
            "http.route": "/api/guilds/{guild_id}/bytes/config",
            "http.method": "GET",
            "client_ip": "203.0.113.7",
        },
        {
            "security.event": "login_failed",
            "success": False,
            "bearer_presented": True,
            "reason": "insufficient_permissions",
            "http.route": "/api/guilds/{guild_id}/bytes/config",
            "http.method": "GET",
            "client_ip": "203.0.113.7",
        },
        {
            "security.event": "rate_limit_exceeded",
            "success": False,
            "key_id": "00000000-0000-0000-0000-0000000000ab",
            "key_prefix": "sk_abcd",
            "current_usage": 10,
            "rate_limit": 10,
            "window": "second",
            "http.route": "/api/guilds/{guild_id}/bytes/config",
            "http.method": "GET",
        },
        {
            "security.event": "admin_operation",
            "success": True,
            "operation": "view_help_conversation",
            "key_id": "00000000-0000-0000-0000-0000000000ab",
            "key_prefix": "sk_abcd",
            "details": "Viewed conversation 00000000-0000-0000-0000-0000000000cd",
            "http.route": "/api/guilds/{guild_id}/bytes/config",
            "http.method": "GET",
        },
    ]
    messages = [
        span["attributes"]["logfire.msg"]
        for span in capfire.exporter.exported_spans_as_dict()
    ]
    assert messages == [
        "Security event: login_failed",
        "Security event: login_failed",
        "Security event: rate_limit_exceeded",
        "Security event: admin_operation",
    ]
    assert "Scrubbed" not in repr(capfire.exporter.exported_spans_as_dict())


def _api_native_route_templates() -> list[str]:
    """Every route template the app mounts from ``api_native`` (app.yaml)."""
    import importlib

    import yaml
    from litestar import Litestar

    config = yaml.safe_load((REPO_ROOT / "app.yaml").read_text())

    def references(node):
        if isinstance(node, str) and node.startswith("smarter_dev.web.api_native."):
            yield node
        elif isinstance(node, dict):
            for child in node.values():
                yield from references(child)
        elif isinstance(node, list):
            for child in node:
                yield from references(child)

    handlers = []
    for reference in references(config):
        module_name, attribute = reference.split(":")
        handlers.append(getattr(importlib.import_module(module_name), attribute))
    app = Litestar(route_handlers=handlers, openapi_config=None)
    return sorted({route.path_format for route in app.routes})


def _request_for(template: str) -> MagicMock:
    request = _request()
    request.scope = {**request.scope, "path_template": template}
    return request


@pytest.mark.asyncio
async def test_no_api_native_route_is_scrubbed_from_any_event(monkeypatch, capfire):
    """Templates like ``/api/auth/status`` contain scrubbed words; the route
    must still arrive intact on every event kind."""
    templates = _api_native_route_templates()
    assert "/api/auth/status" in templates and "/api/auth/validate" in templates
    assert len(templates) > 50  # the whole api_native surface, not a sample
    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: True)
    security = SecurityLogger()

    for template in templates:
        request = _request_for(template)
        await security.log_authentication_failed(
            bearer_presented=True, request=request, reason="no_valid_key"
        )
        await security.log_rate_limit_exceeded(
            api_key=_key(), request=request, current_usage=1, limit=1, window="second"
        )
        await security.log_admin_operation(
            operation="view_help_conversation", api_key=_key(), request=request
        )

    spans = capfire.exporter.exported_spans_as_dict()
    assert len(spans) == 3 * len(templates)
    assert [span["attributes"]["http.route"] for span in spans] == [
        template for template in templates for _ in range(3)
    ]
    assert "Scrubbed" not in repr(spans)
    assert "Auth Service" not in repr(spans)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"), [("post", "/api/auth/validate"), ("get", "/api/auth/status")]
)
async def test_failed_login_on_auth_routes_arrives_intact(
    monkeypatch, capfire, method, path
):
    """The real AuthController behind the real guard, into real Logfire."""
    from litestar.testing import create_test_client

    from smarter_dev.web.api_native.auth import AuthController

    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: True)
    with create_test_client(route_handlers=[AuthController]) as client:
        response = getattr(client, method)(
            path, headers={"Authorization": "Bearer sk-not-a-key"}
        )

    assert response.status_code == 401
    (span,) = [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if "security" in span["attributes"].get("logfire.tags", ())
    ]
    assert span["attributes"]["security.event"] == "login_failed"
    assert span["attributes"]["http.route"] == path
    assert span["attributes"]["reason"] == "no_valid_key"
    assert "Scrubbed" not in repr(span)


@pytest.mark.asyncio
async def test_logfire_level_is_warn_for_failures(monkeypatch, capfire):
    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: True)

    await _fail_auth()

    (span,) = capfire.exporter.exported_spans_as_dict()
    assert span["attributes"]["logfire.level_num"] == 13  # warn


@pytest.mark.asyncio
async def test_goes_to_the_standard_logger_without_logfire(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    fake_logfire = MagicMock()
    monkeypatch.setattr(security_logger_module, "logfire", fake_logfire)
    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: False)

    await _fail_auth()

    fake_logfire.log.assert_not_called()
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.security_event["security.event"] == "login_failed"


@pytest.mark.asyncio
async def test_falls_back_to_the_standard_logger_when_logfire_raises(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    fake_logfire = MagicMock()
    fake_logfire.log.side_effect = RuntimeError("exporter down")
    monkeypatch.setattr(security_logger_module, "logfire", fake_logfire)
    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: True)

    await _fail_auth()

    events = [r for r in caplog.records if hasattr(r, "security_event")]
    assert [r.security_event["security.event"] for r in events] == [
        "login_failed"
    ]


def test_retention_doc_describes_security_events_as_logs():
    doc = (REPO_ROOT / "docs" / "data-retention.md").read_text()
    section = doc.split("## Security logs", 1)[1].split("\n## ", 1)[0]
    assert "structured logs, not database rows" in section
    assert "No event\nrecords a member's Discord id" in section
    assert "never the\nconcrete path" in section
    assert "Logfire" in section
    assert "kept there for 30 days" in section
    assert "is no longer written" in section
    for name in ("`login_failed`", "`rate_limit_exceeded`", "`admin_operation`", "`key_id`", "`http.route`"):
        assert name in section
