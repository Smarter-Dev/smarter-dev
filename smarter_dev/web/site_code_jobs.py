"""Agent-worker job that runs one site chat ``run_code`` call (see ``site_code``).

The web pod submits ``site_code.run`` with only a lightweight descriptor
(``chat.dispatch._SUBMISSION_DESCRIPTORS``); this module loads in the
agent-worker, beside the chat jobs whose ``run_code`` it shares.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from skrift.workers import handler

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web import site_code
from smarter_dev.web.chat.toolsets import run_code

logger = logging.getLogger(__name__)


class SiteCodePayload(BaseModel):
    run_id: str


# max_attempts matches the web pod's submission descriptor in chat.dispatch. A
# retry is harmless: the first attempt took the snippet, so a retry runs nothing.
@handler(
    site_code.JOB_TYPE,
    queue=site_code.JOB_QUEUE,
    max_attempts=5,
    visibility_timeout=site_code.JOB_VISIBILITY_TIMEOUT,
)
async def run_site_code_job(payload: SiteCodePayload) -> dict:
    redis = get_redis_client()
    request = await site_code.take_request(redis, payload.run_id)
    if request is None:
        return {"status": "expired"}
    # run_code never raises: every failure comes back as text for the model.
    text = await run_code(request["reason"], request["code"])
    await site_code.push_result(redis, payload.run_id, text)
    return {"status": "complete"}
