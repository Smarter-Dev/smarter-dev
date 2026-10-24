"""Agent-worker job that runs one site chat ``run_code`` call (see ``site_code``).

The web pod submits ``site_code.run`` with only a lightweight descriptor
(``chat.dispatch._SUBMISSION_DESCRIPTORS``); this module loads in the
agent-worker, beside the chat jobs whose ``run_code`` it shares.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from pydantic import BaseModel
from skrift.workers import handler

from smarter_dev.shared.code_sandbox import RunNotWanted
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
    run_id = payload.run_id
    request = await site_code.take_request(redis, run_id)
    if request is None:
        return {"status": "expired"}

    async def wanted() -> bool:
        return await site_code.still_wanted(redis, run_id, request["deadline"])

    # run_code never raises for the code's own failures: they come back as
    # text for the model. It raises RunNotWanted when the route gave up
    # while the run waited for a place, before any sandbox started.
    run = asyncio.ensure_future(
        run_code(request["reason"], request["code"], still_wanted=wanted)
    )

    abandoned = False

    async def keep_alive() -> None:
        # The route watches the heartbeat; an abandoned run stops here,
        # sandbox and all.
        nonlocal abandoned
        while not run.done():
            await site_code.heartbeat(redis, run_id)
            if not await wanted():
                abandoned = True
                run.cancel()
                return
            await asyncio.sleep(site_code.HEARTBEAT_SECONDS)

    watcher = asyncio.ensure_future(keep_alive())
    try:
        text = await run
    except RunNotWanted:
        return {"status": "abandoned"}
    except asyncio.CancelledError:
        if not abandoned:
            raise  # the job itself was cancelled
        return {"status": "abandoned"}
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await site_code.finish(redis, run_id)
    await site_code.push_result(redis, run_id, text)
    return {"status": "complete"}
