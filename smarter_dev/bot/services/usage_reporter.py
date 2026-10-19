"""The bot's side-agent spend, posted to the usage ledger.

A chat turn's own model usage travels with the turn and a proactive wake's
with the wake. Everything else the bot runs (the length summarizer, the chat
tools' sub-agents, the utility agents, the rule matcher, handler authoring,
privacy purges) is metered here::

    async with metered("rule_matcher", guild_id=...) as meter:
        result = await agent.run(prompt, model=meter.wrap(agent.model))

Every provider response the run gets is counted as it arrives, so a run that
raises still reports what the provider billed. The report goes to
``POST /api/bot-usage`` in the background: a side agent never waits on the
ledger, and a lost report is logged, never raised. A report the web tier
cannot take yet (a 404 from a web pod older than the endpoint during a
rollout, a 5xx, no connection) is sent again with backoff for about ten
minutes. The run's id is made once and every attempt carries it, so the
ledger writes one row however many attempts reach it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.exceptions import AuthenticationError
from smarter_dev.bot.services.exceptions import NetworkError
from smarter_dev.bot.services.exceptions import RateLimitError
from smarter_dev.bot.services.exceptions import ResourceNotFoundError
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.usage_meter import UsageMeter

logger = logging.getLogger(__name__)

_api_client = None
_pending: set[asyncio.Task] = set()
# The waits before each attempt after the first: 615 s in all, so a report
# outlasts a rollout's window where bots are new and web pods are not.
RETRY_DELAYS = (5, 10, 20, 40, 80, 160, 300)
# Set at shutdown: reports waiting to retry make one last attempt now.
_stopping = False


def configure_usage_reporter(api_client) -> None:
    """Set the client reports go out on; the bot does this at startup."""
    global _api_client, _stopping
    _api_client = api_client
    _stopping = False


def usage_payload(
    meter: UsageMeter,
    *,
    operation_type: str,
    operation_id: str,
    metered_at: datetime,
    discord_user_id: str | int | None = None,
    guild_id: str | int | None = None,
    channel_id: str | int | None = None,
    engagement_id: UUID | str | None = None,
    reasoning_level: str | None = None,
    failed: bool = False,
) -> dict:
    def text(value) -> str | None:
        return None if value is None else str(value)

    return {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "metered_at": metered_at.isoformat(),
        "discord_user_id": text(discord_user_id),
        "guild_id": text(guild_id),
        "channel_id": text(channel_id),
        "engagement_id": text(engagement_id),
        "reasoning_level": reasoning_level,
        "failed": failed,
        "stages": meter.stage_dicts(),
    }


def _retryable(error: Exception) -> bool:
    """Whether the web tier may take the report later: no connection, a
    5xx, a rate limit, or a 404 (a web pod without the endpoint yet). A
    report it refused (a 4xx such as a validation error) never will be."""
    if isinstance(error, AuthenticationError):
        return False
    if isinstance(error, NetworkError | RateLimitError | ResourceNotFoundError):
        return True
    if isinstance(error, APIError):
        status = error.status_code
        return status is None or status == 404 or status >= 500
    return isinstance(error, OSError)


async def _wait(delay: float) -> None:
    """Sleep *delay* seconds, or until shutdown asks for a last attempt."""
    loop = asyncio.get_running_loop()
    until = loop.time() + delay
    while not _stopping and (left := until - loop.time()) > 0:
        await asyncio.sleep(min(left, 1.0))


async def _post(payload: dict) -> None:
    delays = iter(RETRY_DELAYS)
    while True:
        try:
            await _api_client.post("/bot-usage", json_data=payload)
            return
        except Exception as error:  # noqa: BLE001 — the ledger never breaks a feature
            delay = next(delays, None)
            if delay is not None and not _stopping and _retryable(error):
                logger.warning(
                    "%s usage (%s) not recorded yet (%s); retrying in %ss",
                    payload["operation_type"],
                    payload["operation_id"],
                    type(error).__name__,
                    delay,
                )
                await _wait(delay)
                continue
            log_exception(
                logger,
                "failed to record %s usage (%s)",
                payload["operation_type"],
                payload["operation_id"],
            )
            return


def report_usage(meter: UsageMeter, *, operation_type: str, **fields) -> None:
    """Post what *meter* saw, in the background. Nothing to report, or no
    client configured (tests, evals), posts nothing."""
    if not meter:
        return
    if _api_client is None:
        logger.debug(
            "no usage reporter configured; %s usage not recorded", operation_type
        )
        return
    fields.setdefault("operation_id", uuid4().hex)
    fields.setdefault("metered_at", datetime.now(UTC))
    payload = usage_payload(meter, operation_type=operation_type, **fields)
    task = asyncio.get_running_loop().create_task(_post(payload))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


@asynccontextmanager
async def metered(operation_type: str, **fields) -> AsyncIterator[UsageMeter]:
    """A meter for the runs in the block, reported when the block ends,
    whether or not it raised."""
    meter = UsageMeter()
    metered_at = datetime.now(UTC)
    try:
        yield meter
    except BaseException:
        report_usage(
            meter,
            operation_type=operation_type,
            metered_at=metered_at,
            failed=True,
            **fields,
        )
        raise
    report_usage(meter, operation_type=operation_type, metered_at=metered_at, **fields)


async def drain_usage_reports(*, stopping: bool = False) -> None:
    """Wait for the reports in flight (bot shutdown, tests). *stopping*: the
    bot is going away, so reports waiting to retry try once more now rather
    than after their backoff."""
    global _stopping
    if stopping:
        _stopping = True
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
