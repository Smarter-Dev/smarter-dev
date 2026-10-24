"""Site service route that runs one chat ``run_code`` call for a member.

The site Worker's chat model calls ``run_code`` with a short reason and a
Python snippet. The Worker never runs code: it posts both here with the site
service key, naming the member in the path, and gets back the text the model
reads, exactly what smarter.dev's own chat ``run_code`` returns for the same
call. The run happens on the agent worker (``smarter_dev.web.site_code``).

Errors: 404 for an unknown member, 429 while the member or the service has
too many runs going (with ``Retry-After``), 504 when no result came back in
time, 503 when the run couldn't be queued. Each is a plain sentence.
"""

from __future__ import annotations

import logging
from uuid import UUID
from uuid import uuid4

from litestar import Controller
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
        self, db_session: AsyncSession, user_id: str, data: RunCode
    ) -> dict[str, str]:
        user = await _member(db_session, user_id)
        member: UUID = user.id
        # Release the request's connection before the long wait below.
        await db_session.close()
        redis = get_redis_client()
        run_id = uuid4().hex
        refused = await site_code.admit(redis, member, run_id)
        if refused:
            raise BotApiException(429, {"detail": refused, "wait": 5})
        try:
            await site_code.store_request(
                redis, run_id, reason=data.reason, code=data.code
            )
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
                logger.error(
                    "Site code run couldn't be queued (%s)", type(error).__name__
                )
                await site_code.abandon(redis, run_id)
                raise plain_error(503, START_FAILED) from None
            text = await site_code.wait_for_result(
                redis, run_id, site_code.WAIT_SECONDS
            )
            if text is None:
                await site_code.abandon(redis, run_id)
                raise plain_error(504, site_code.TIMED_OUT)
            return {"result": text}
        finally:
            await site_code.release(redis, member, run_id)
