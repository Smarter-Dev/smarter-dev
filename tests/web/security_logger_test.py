"""Security events go to Logfire when it is on, else the standard logger (#81)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from smarter_dev.web import security_logger as security_logger_module
from smarter_dev.web.security_logger import SecurityLogger


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
        created_by="discord-bot",
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
        user_identifier="bot:discord-bot",
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
            "route": "/api/guilds/{guild_id}/bytes/config",
            "method": "GET",
            "client_ip": "203.0.113.7",
        },
        {
            "security.event": "login_failed",
            "success": False,
            "bearer_presented": True,
            "reason": "insufficient_permissions",
            "route": "/api/guilds/{guild_id}/bytes/config",
            "method": "GET",
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
            "route": "/api/guilds/{guild_id}/bytes/config",
            "method": "GET",
        },
        {
            "security.event": "admin_operation",
            "success": True,
            "operation": "view_help_conversation",
            "caller": "bot:discord-bot",
            "details": "Viewed conversation 00000000-0000-0000-0000-0000000000cd",
            "route": "/api/guilds/{guild_id}/bytes/config",
            "method": "GET",
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
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "data-retention.md").read_text()
    section = doc.split("## Security logs", 1)[1].split("\n## ", 1)[0]
    assert "structured logs, not database rows" in section
    assert "No event\nrecords a member's Discord id" in section
    assert "never the\nconcrete path" in section
    assert "Logfire" in section
    assert "is no longer written" in section
    for name in ("`login_failed`", "`rate_limit_exceeded`", "`admin_operation`", "`key_id`"):
        assert name in section
