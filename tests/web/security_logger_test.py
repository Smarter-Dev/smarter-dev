"""Security events go to Logfire when it is on, else the standard logger (#81)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from smarter_dev.web import security_logger as security_logger_module
from smarter_dev.web.security_logger import SecurityLogger


def _request() -> MagicMock:
    request = MagicMock()
    request.client = SimpleNamespace(host="203.0.113.7")
    request.method = "GET"
    request.scope = {"path_template": "/api/guilds/{guild_id}/bytes/config"}
    return request


async def _fail_auth() -> None:
    await SecurityLogger().log_authentication_failed(
        bearer_presented=False, request=_request(), reason="missing"
    )


@pytest.mark.asyncio
async def test_goes_to_logfire_when_it_is_configured(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    fake_logfire = MagicMock()
    monkeypatch.setattr(security_logger_module, "logfire", fake_logfire)
    monkeypatch.setattr(security_logger_module, "logfire_enabled", lambda: True)

    await _fail_auth()

    fake_logfire.log.assert_called_once()
    level, template = fake_logfire.log.call_args.args
    attributes = fake_logfire.log.call_args.kwargs["attributes"]
    assert (level, template) == ("warn", "Security event: {security.event}")
    assert attributes == {
        "security.event": "authentication_failed",
        "success": False,
        "bearer_presented": False,
        "reason": "missing",
        "route": "/api/guilds/{guild_id}/bytes/config",
        "method": "GET",
        "client_ip": "203.0.113.7",
    }
    assert caplog.records == []


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
    assert record.security_event["security.event"] == "authentication_failed"


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
        "authentication_failed"
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
