"""A usage report outlasts a rollout's gap: the web tier answering 404 (a pod
older than ``/api/bot-usage``), a 5xx or no connection (#211).

The report goes out through the real APIClient over an httpx MockTransport;
what reached the "web tier" is then fed through the real BotUsageController
into the sqlite ledger. The backoff's waits are recorded, not slept.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic_ai.usage import RequestUsage

from smarter_dev.bot.services import usage_reporter
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.api_client import RetryConfig
from smarter_dev.shared.request_usage import usage_tokens
from smarter_dev.shared.usage_meter import UsageMeter
from tests.bot.side_agent_usage_test import _land

KEY = "sk_" + "a" * 40


def _meter() -> UsageMeter:
    meter = UsageMeter()
    meter.add(
        "claude-haiku-5-5",
        usage_tokens(RequestUsage(input_tokens=1_000, output_tokens=20)),
    )
    return meter


@pytest.fixture
def web(monkeypatch):
    """The web tier's answers, in order, and every body it was sent."""
    world = {"answers": [], "bodies": [], "waits": []}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/bot-usage"
        world["bodies"].append(json.loads(request.content))
        answer = world["answers"].pop(0) if world["answers"] else 200
        if answer == "down":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(answer, json={"detail": "x", "recorded": 1})

    client = APIClient(
        base_url="http://web.test/api",
        api_key=KEY,
        retry_config=RetryConfig(max_retries=0),
    )
    client._client = httpx.AsyncClient(
        base_url="http://web.test/api", transport=httpx.MockTransport(handle)
    )

    async def wait(delay):
        world["waits"].append(delay)

    monkeypatch.setattr(usage_reporter, "_wait", wait)
    usage_reporter.configure_usage_reporter(client)
    yield world
    usage_reporter.configure_usage_reporter(None)


async def test_a_report_retries_through_a_rollout_and_lands_once(web, db_session):
    # An old web pod (404), one restarting (503), none reachable, then the
    # new endpoint — which writes the row but whose answer is lost (a 502
    # from the proxy), so the report is sent once more.
    web["answers"] = [404, 404, 503, "down", 502]

    usage_reporter.report_usage(_meter(), operation_type="tldr", guild_id="1")
    await usage_reporter.drain_usage_reports()

    assert len(web["bodies"]) == 6
    assert web["waits"] == list(usage_reporter.RETRY_DELAYS[:5])
    # Every attempt is the same report, under the same id.
    assert {json.dumps(body, sort_keys=True) for body in web["bodies"]} == {
        json.dumps(web["bodies"][0], sort_keys=True)
    }
    rows = []
    for body in web["bodies"][4:]:  # the attempts the new endpoint received
        rows = await _land(db_session, body)
    assert len(rows) == 1
    assert rows[0].input_tokens == 1_000


async def test_retries_cover_ten_minutes_then_give_up(web):
    web["answers"] = [404] * 100

    usage_reporter.report_usage(_meter(), operation_type="tldr")
    await usage_reporter.drain_usage_reports()

    assert sum(web["waits"]) >= 600
    assert len(web["bodies"]) == len(usage_reporter.RETRY_DELAYS) + 1


async def test_a_refused_report_is_not_retried(web):
    # Negative control: a 422 will never be accepted, however often it is sent.
    web["answers"] = [422]

    usage_reporter.report_usage(_meter(), operation_type="tldr")
    await usage_reporter.drain_usage_reports()

    assert len(web["bodies"]) == 1
    assert web["waits"] == []


async def test_shutdown_makes_a_waiting_report_try_once_more_now(monkeypatch):
    sent = []

    class Api:
        async def post(self, path, json_data=None, **_):
            sent.append(json_data)
            if len(sent) == 1:
                raise usage_reporter.NetworkError("down")

    usage_reporter.configure_usage_reporter(Api())
    try:
        usage_reporter.report_usage(_meter(), operation_type="tldr")
        # The real wait: the first attempt failed and the report is backing
        # off for RETRY_DELAYS[0] seconds when the bot shuts down.
        await asyncio.sleep(0.05)
        assert len(sent) == 1
        await asyncio.wait_for(
            usage_reporter.drain_usage_reports(stopping=True), timeout=3
        )
    finally:
        usage_reporter.configure_usage_reporter(None)

    assert len(sent) == 2
