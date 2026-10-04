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

import httpx
import pytest

from smarter_dev.shared import email as email_module
from smarter_dev.web import research_tools
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
    request.scope = {
        "path_template": "/api/guilds/{guild_id}/members",
        "state": {"client_ip": "203.0.113.7"},
    }
    request.method = "GET"
    request.query_params = query or {}
    return request


def _session() -> MagicMock:
    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_rejected_bearer_leaves_no_fragment_in_the_event(caplog):
    caplog.set_level(logging.INFO)

    await SecurityLogger().log_authentication_failed(
        bearer_presented=True,
        request=_request(),
        reason="no_valid_key",
    )

    (record,) = caplog.records
    event = record.security_event
    assert event["security.event"] == "login_failed"
    assert event["bearer_presented"] is True
    assert event["route"] == "/api/guilds/{guild_id}/members"
    assert "failed_key_prefix" not in event
    assert "***" not in caplog.text


@pytest.mark.asyncio
async def test_rejected_bearer_through_real_guard_and_logger(monkeypatch):
    """One rejected request, real guard and real security logger, no DB write."""
    from litestar.di import Provide
    from litestar.plugins.pydantic import PydanticPlugin
    from litestar.testing import create_test_client
    from sqlalchemy.ext.asyncio import AsyncSession

    from smarter_dev.shared import database
    from smarter_dev.web.api_native.bytes import BytesController

    stored = _session()

    async def fake_db_session():
        yield stored

    monkeypatch.setattr(database, "get_db_session", fake_db_session)
    # Litestar's dictConfig replaces root handlers, so capture on the logger.
    records: list[logging.LogRecord] = []
    capture = logging.Handler()
    capture.emit = records.append
    stdout_logger = logging.getLogger("smarter_dev.web.security_logger.SecurityLogger")
    stdout_logger.addHandler(capture)
    monkeypatch.setattr(stdout_logger, "level", logging.INFO)

    try:
        client_cm = create_test_client(
            route_handlers=[BytesController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(
                    lambda: AsyncMock(spec=AsyncSession), sync_to_thread=False
                )
            },
        )
        with client_cm as client:
            response = client.get(
                "/api/guilds/123456789012345678/bytes/config",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
    finally:
        stdout_logger.removeHandler(capture)
    logged = "\n".join(record.getMessage() for record in records)

    assert response.status_code == 401
    stored.add.assert_not_called()  # events are logs, not rows (#81)
    (record,) = records
    event = record.security_event
    assert event["security.event"] == "login_failed"
    assert event["bearer_presented"] is True
    # The route template, never the concrete path with its Discord ids (#81).
    assert event["route"] == "/api/guilds/{guild_id}/bytes/config"
    fragment = TOKEN[:6]
    assert "Security event: login_failed" in logged
    assert fragment not in logged
    assert "123456789012345678" not in logged


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


def _failing_client(status: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    )


@pytest.mark.asyncio
async def test_research_tool_failures_log_no_url_query_or_key(caplog, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key-value")
    monkeypatch.setenv("YOUTUBE_API_KEY", "youtube-test-key-value")
    monkeypatch.setenv("JINA_API_KEY", "jina-test-key-value")
    caplog.set_level(logging.DEBUG, logger=research_tools.__name__)

    async with _failing_client(403) as client:
        await research_tools.brave_search(client, SEARCH)
        await research_tools.youtube_search(client, SEARCH)
        await research_tools.youtube_video_details(client, ["abcdefghijk"])

    def raise_connect(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(raise_connect)) as client:
        await research_tools.jina_search(client, SEARCH)
        await research_tools.jina_read(client, f"https://example.org/p?q={SEARCH}")
        await research_tools.fetch_og_metadata(
            client, f"https://example.org/p?q={SEARCH}"
        )

    # httpx's own INFO lines are main.py's concern (see the entry point test).
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == research_tools.__name__
    )
    assert "HTTPStatusError 403" in logged
    assert "ConnectError" in logged
    assert "Jina Reader failed for example.org: ConnectError" in logged
    assert "OG fetch failed for example.org" in logged
    for secret in (SEARCH, "test-key-value", "abcdefghijk", "/p?"):
        assert secret not in logged


@pytest.mark.asyncio
async def test_research_tool_failure_logging_survives_malformed_urls(caplog):
    """A URL urlparse rejects must still reach the tools' fallbacks."""
    caplog.set_level(logging.DEBUG, logger=research_tools.__name__)
    malformed = f"http://[{SEARCH}/p"

    def raise_connect(request):
        raise httpx.ConnectError("cannot connect", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(raise_connect)) as client:
        read = await research_tools.jina_read(client, malformed)
        og = await research_tools.fetch_og_metadata(client, malformed)

    assert "error" in read
    assert og == {}
    assert SEARCH not in "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == research_tools.__name__
    )

