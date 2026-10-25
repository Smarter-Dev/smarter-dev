"""Site service route that runs one chat ``run_code`` call for a member.

The site Worker's chat model calls ``run_code`` with a short reason and a
Python snippet. The Worker never runs code: it posts both here with the site
service key, naming the member in the path, and gets back the text the model
reads, exactly what smarter.dev's own chat ``run_code`` returns for the same
call. The run happens on the agent worker (``smarter_dev.web.site_code``).

Errors: 404 for an unknown member, 429 while the member or the service has
too many runs going (with ``Retry-After``), 504 when no result came back in
time, 503 when the run couldn't be admitted or queued, or its worker stopped
without a result. Each is a plain sentence.

A caller that hangs up stops its run: Hypercorn doesn't cancel the route when
the connection closes, it only says so on the request's ``receive``, so the
route watches for that from admission on, while the job is queued and while
it waits for the result.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Coroutine
from typing import Any
from uuid import UUID
from uuid import uuid4

from litestar import Controller
from litestar import Request
from litestar import post
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web import site_code
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import BotApiException
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.api_native.site_search import _error_response
from smarter_dev.web.api_native.site_search import _member

logger = logging.getLogger(__name__)

SITE_CODE_GUARDS = [*SITE_API_GUARDS, Permission("site-code")]

START_FAILED = "Code running is unavailable right now."
# The caller closed its connection; nobody reads this reply.
HUNG_UP = "The request was closed before the run finished."


class RunCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Blank values pass through so the run says what Python's tool says.
    reason: str = Field(max_length=site_code.MAX_REASON_CHARS)
    code: str = Field(max_length=site_code.MAX_CODE_CHARS)


class SiteCodeController(Controller):
    path = "/api/site/code"
    response_headers = {"Cache-Control": "private, no-store"}
    request_max_body_size = site_code.MAX_BODY_BYTES
    exception_handlers = {
        **dict.fromkeys(BOT_API_EXCEPTION_HANDLERS, _error_response),
        HTTPException: _error_response,
        Exception: _error_response,
    }

    @post(
        "/users/{user_id:str}/runs",
        guards=SITE_CODE_GUARDS,
        status_code=HTTP_200_OK,
    )
    async def run(
        self, request: Request, db_session: AsyncSession, user_id: str, data: RunCode
    ) -> dict[str, str]:
        user = await _member(db_session, user_id)
        member: UUID = user.id
        # Release the request's connection before the long wait below.
        await db_session.close()
        redis = get_redis_client()
        run_id = uuid4().hex
        try:
            refused = await site_code.admit(redis, member, run_id)
        except Exception as error:  # noqa: BLE001 - a plain 503 for the site
            logger.error("Site code admission failed (%s)", type(error).__name__)
            raise plain_error(503, START_FAILED) from None
        if refused:
            raise BotApiException(429, {"detail": refused, "wait": 5})
        finished = False
        # Watched from here on: Hypercorn says the caller left only on
        # ``receive``, and never cancels the route.
        gone = asyncio.ensure_future(_gone(request))
        tasks = [gone]
        try:
            await site_code.store_request(
                redis, run_id, reason=data.reason, code=data.code
            )
            if gone.done():
                raise plain_error(499, HUNG_UP)
            submitting = asyncio.ensure_future(_submit(run_id))
            tasks.append(submitting)
            if await _left_first(gone, submitting):
                # The caller left while the job was being queued: drop the
                # call now, before a job can take it, then let the queueing
                # finish (its job finds nothing to run) before the places
                # are given back.
                await _drop(redis, run_id)
                await asyncio.gather(submitting, return_exceptions=True)
                raise plain_error(499, HUNG_UP)
            submitting.result()
            waiting = asyncio.ensure_future(_result(redis, run_id))
            tasks.append(waiting)
            if await _left_first(gone, waiting):
                raise plain_error(499, HUNG_UP)
            text = waiting.result()
            finished = True
            return {"result": text}
        finally:
            for task in tasks:
                task.cancel()
            # Given up for any reason (504, a dead worker, an error, the
            # caller gone, the request cancelled): the job must not run it,
            # or stops it.
            await _held(_unwind(redis, member, run_id, tasks, drop=not finished))


async def _unwind(
    redis, member: UUID, run_id: str, tasks: list[asyncio.Future], *, drop: bool
) -> None:
    """Drop the run if it was given up, wait for the route's own tasks to
    end, then give both places back.

    The places are given back last: a cancelled queueing still unwinding
    (its database session closing) has not settled whether a job exists,
    and its place stays taken until it has. A run dropped first leaves that
    job nothing to run."""
    if drop:
        await _drop(redis, run_id)
    await asyncio.gather(*tasks, return_exceptions=True)
    with contextlib.suppress(Exception):
        await site_code.release(redis, member, run_id)


async def _held(work: Coroutine[Any, Any, None]) -> None:
    """Run ``work`` to its end even if the route is cancelled meanwhile;
    that cancellation is raised once it has."""
    task = asyncio.ensure_future(work)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        raise cancelled
    task.result()


async def _submit(run_id: str) -> None:
    """Queue the run's job; a plain 503 if it can't be queued."""
    try:
        from skrift.workers import submit as worker_submit

        from smarter_dev.web.chat.dispatch import ensure_submission_handler

        ensure_submission_handler(site_code.JOB_TYPE)
        await worker_submit(
            site_code.JOB_TYPE,
            {"run_id": run_id},
            queue=site_code.JOB_QUEUE,
            job_id=f"site-code:{run_id}",
        )
    except Exception as error:  # noqa: BLE001 - a plain 503 for the site
        # Type only: never the snippet or the reason.
        logger.error("Site code run couldn't be queued (%s)", type(error).__name__)
        raise plain_error(503, START_FAILED) from None


async def _drop(redis, run_id: str) -> None:
    """Abandon the run: the job never starts it, or stops it."""
    with contextlib.suppress(Exception):
        await asyncio.shield(site_code.abandon(redis, run_id))


async def _result(redis, run_id: str) -> str:
    """The run's text, watching that a job that took the call stays alive."""
    deadline = time.monotonic() + site_code.WAIT_SECONDS
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise plain_error(504, site_code.TIMED_OUT)
        text = await site_code.wait_for_result(
            redis, run_id, min(site_code.HEARTBEAT_SECONDS, left)
        )
        if text is not None:
            return text
        if await site_code.worker_gone(redis, run_id):
            logger.error("Site code run's worker stopped without a result")
            raise plain_error(503, site_code.STOPPED)


async def _gone(request: Request) -> None:
    """Return once the caller has closed its connection.

    The body is already read, so the next message is ``http.disconnect``.
    """
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _left_first(gone: asyncio.Future, task: asyncio.Future) -> bool:
    """Wait for ``task`` or for the caller to leave; whether it left first."""
    await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
    return not task.done()
