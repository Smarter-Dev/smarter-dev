"""Agent-worker jobs for a chat bot purge: the run, and the check after it.

The admin page and the bot API submit these from the web pod, which imports
this module only to register the job types; it is light on purpose. The purge
agent and its model stack load inside :func:`run_chat_bot_purge`, in the
agent-worker.

Neither job retries on its own: a purge step that fails stays failed and
visible to the admin, who runs it again from the request page. Both jobs are
safe to repeat.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from skrift.workers import handler
from skrift.workers import submit as worker_submit

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.chat_bot_purge import STATUS_CHECKING
from smarter_dev.web.chat_bot_purge import run_check
from smarter_dev.web.chat_bot_purge import run_purge

logger = logging.getLogger(__name__)


class PurgeJobFailed(RuntimeError):
    """A purge job failed; the message is the original error's type only.

    Database and validation errors quote statement parameters and values,
    which here include the target's Discord ID and names. The job runtime
    logs and stores whatever a handler raises, so the handler never lets the
    original error out.
    """


class ChatBotPurgePayload(BaseModel):
    request_id: UUID
    run_id: UUID


class ChatBotPurgeCheckPayload(BaseModel):
    request_id: UUID


def _now() -> datetime:
    return datetime.now(UTC)


# The run waits up to ten minutes for both runtimes to enforce the block list,
# then a few model calls per guild. A run can outlive this: skrift does not
# renew a claim, so another worker may claim the same job again and run it
# concurrently. The run's lease on its request makes that second execution
# return "already_running" without touching anything.
@handler("chat_bot_purge.run", queue="agents", max_attempts=1, visibility_timeout=3600.0)
async def run_chat_bot_purge(payload: ChatBotPurgePayload) -> dict:
    failure = None
    try:
        status = await run_purge(
            payload.request_id,
            payload.run_id,
            session_factory=get_db_session_context,
            redis=get_redis_client(),
            now=_now,
        )
    except Exception as error:  # noqa: BLE001 — re-raised below without its text
        failure = type(error).__name__
    if failure is not None:
        # Raised outside the except block, so it carries no __context__.
        raise PurgeJobFailed(failure)
    if status == STATUS_CHECKING:
        await submit_check(payload.request_id)
    logger.info("Purge run %s ended as %s", payload.run_id, status)
    return {"status": status}


# The check runs the final notes pass (model calls) before its search.
@handler("chat_bot_purge.check", queue="agents", max_attempts=1, visibility_timeout=3600.0)
async def check_chat_bot_purge(payload: ChatBotPurgeCheckPayload) -> dict:
    failure = None
    try:
        status = await run_check(
            payload.request_id,
            session_factory=get_db_session_context,
            redis=get_redis_client(),
            now=_now,
        )
    except Exception as error:  # noqa: BLE001 — re-raised below without its text
        failure = type(error).__name__
    if failure is not None:
        # Raised outside the except block, so it carries no __context__.
        raise PurgeJobFailed(failure)
    return {"status": status}


async def submit_run(request_id: UUID, run_id: UUID) -> None:
    await worker_submit(ChatBotPurgePayload(request_id=request_id, run_id=run_id))


async def submit_check(request_id: UUID) -> None:
    await worker_submit(ChatBotPurgeCheckPayload(request_id=request_id))
