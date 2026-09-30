"""Web and shared log lines carry no emails, credential fragments or query values.

Each test drives the real code path and checks both the stdout record and, for
the security log, the row it would write.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from smarter_dev.shared import email as email_module
from smarter_dev.web.security_logger import SecurityLogger

ADDRESS = "someone@example.invalid"
TOKEN = "sk-" + "q" * 43
SEARCH = "findme-term"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _request(query: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.client = SimpleNamespace(host="203.0.113.7")
    request.headers = {"user-agent": "pytest"}
    request.url.path = "/api/guilds/1/members"
    request.method = "GET"
    request.query_params = query or {}
    return request


def _session() -> MagicMock:
    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def _written_row(session: MagicMock):
    session.add.assert_called_once()
    return session.add.call_args.args[0]


@pytest.mark.asyncio
async def test_rejected_bearer_leaves_no_fragment_in_row_or_stdout(caplog):
    session = _session()
    caplog.set_level(logging.INFO)

    await SecurityLogger().log_authentication_failed(
        session=session,
        bearer_presented=True,
        request=_request(),
        reason="Invalid API key",
    )

    row = _written_row(session)
    assert row.event_metadata["bearer_presented"] is True
    assert "failed_key_prefix" not in row.event_metadata
    stored = f"{row.details} {row.event_metadata}"
    assert TOKEN[:4] not in stored
    assert "presented bearer" in row.details
    assert "***" not in caplog.text


@pytest.mark.asyncio
async def test_api_request_row_keeps_param_names_not_values():
    session = _session()
    api_key = SimpleNamespace(id=uuid4(), key_prefix="skrift_ab", created_by="bot")

    await SecurityLogger().log_api_request(
        session=session,
        api_key=api_key,
        request=_request({"q": SEARCH, "limit": "5"}),
    )

    row = _written_row(session)
    assert row.event_metadata["query_param_names"] == ["limit", "q"]
    assert SEARCH not in repr(row.event_metadata)
    assert SEARCH not in row.details


@pytest.mark.asyncio
async def test_email_logs_never_name_the_recipient(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=email_module.__name__)

    monkeypatch.setattr(email_module, "_ensure_api_key", lambda: False)
    await email_module.send_email(ADDRESS, "subject", "<p>x</p>")

    monkeypatch.setattr(email_module, "_ensure_api_key", lambda: True)
    fake_resend = SimpleNamespace(Emails=SimpleNamespace(send=MagicMock()))
    with patch.dict(sys.modules, {"resend": fake_resend}):
        await email_module.send_email(ADDRESS, "subject", "<p>x</p>")
        fake_resend.Emails.send.side_effect = ValueError(f"bad recipient {ADDRESS}")
        await email_module.send_email(ADDRESS, "subject", "<p>x</p>")

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Skipping email (no API key)",
        "Email sent",
        "Failed to send email: ValueError",
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert ADDRESS not in caplog.text


def test_web_entry_point_caps_httpx_at_warning():
    """Litestar raises the root logger to INFO; httpx must not follow it there."""
    probe = (
        "import logging, main; "
        "print(logging.getLogger().getEffectiveLevel(), "
        "logging.getLogger('httpx').getEffectiveLevel(), "
        "logging.getLogger('smarter_dev').getEffectiveLevel())"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    root, httpx_level, app_level = result.stdout.split()[-3:]
    assert int(root) == logging.INFO
    assert int(httpx_level) == logging.WARNING
    assert int(app_level) == logging.INFO
