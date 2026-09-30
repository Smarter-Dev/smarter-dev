"""Agent-worker job that runs one dashboard web search.

The web pod submits ``web_search.run`` through the dispatch outbox with only a
lightweight descriptor (``chat.dispatch._SUBMISSION_DESCRIPTORS``); this
module, and the pydantic-ai stack it pulls in, loads only in the agent-worker.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel
from skrift.db.models.user import User
from skrift.notifications import NotificationMode
from skrift.notifications import notify_user
from skrift.workers import handler

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search.snapshot import EVENT_TYPE

logger = logging.getLogger(__name__)


class WebSearchPayload(BaseModel):
    search_id: str


async def notify_progress(owner_user_id: UUID, state: dict) -> None:
    """Push the search's snapshot to every open tab of its owner.

    Ephemeral: a tab that misses one re-reads the row when it reconnects."""
    try:
        await notify_user(
            str(owner_user_id), EVENT_TYPE, mode=NotificationMode.EPHEMERAL, search=state
        )
    except Exception as error:
        # Type only: the payload holds the user's request and results.
        logger.error(
            "Web search notification failed after its state was saved (%s)",
            type(error).__name__,
        )


# max_attempts matches the web pod's submission descriptor in chat.dispatch.
@handler("web_search.run", queue="agents", max_attempts=5, visibility_timeout=300.0)
async def run_web_search_job(payload: WebSearchPayload) -> dict:
    from smarter_dev.web.web_search.pipeline import run_search

    search_id = UUID(payload.search_id)
    async with get_db_session_context() as session:
        run = await session.get(WebSearchRun, search_id)
        if run is None:
            return {"status": "missing"}
        if run.status in {"complete", "error"}:
            return {"status": run.status, "idempotent": True}
        owner = await session.get(User, run.owner_user_id)
        if owner is None or not owner.is_active:
            run.status = "error"
            run.error = "The account is inactive."
            await session.commit()
            return {"status": "error"}
    return {"status": await run_search(search_id, notify_progress)}
