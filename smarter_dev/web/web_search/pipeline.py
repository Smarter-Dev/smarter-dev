"""Run one dashboard web search, saving and announcing every stage.

Luna writes five queries, Brave runs each one, and Jev ranks the unique
results. After each step the row is committed and then the owner is notified
with the whole snapshot, so the browser can render any event on its own and a
reload paints from the row.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from uuid import UUID

import httpx
from sqlalchemy import select

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search import brave
from smarter_dev.web.web_search import metering
from smarter_dev.web.web_search.snapshot import snapshot

logger = logging.getLogger(__name__)

Notify = Callable[[UUID, dict], Awaitable[None]]


class _Run:
    """The search's row, re-read and committed for every step."""

    def __init__(self, run_id: UUID, notify: Notify) -> None:
        self.run_id = run_id
        self.notify = notify

    async def update(self, change: Callable[[WebSearchRun], None]) -> dict:
        async with get_db_session_context() as session:
            run = await session.scalar(
                select(WebSearchRun).where(WebSearchRun.id == self.run_id).with_for_update()
            )
            change(run)
            run.version += 1
            await session.commit()
            await session.refresh(run)
            state = snapshot(run)
            owner = run.owner_user_id
        await self.notify(owner, state)
        return state


def _merge(queries: list[dict]) -> list[dict]:
    """Unique results across the queries, in the order Brave found them.

    A page found by several queries keeps its first snippet and lists every
    query that found it."""
    merged: dict[str, dict] = {}
    for index, item in enumerate(queries):
        for hit in item.get("hits", []):
            if hit["url"] in merged:
                merged[hit["url"]]["queries"].append(index)
            else:
                merged[hit["url"]] = {**hit, "queries": [index]}
    return list(merged.values())


def _brave_failure(error: Exception) -> str:
    # BraveError text is only ever the status or a missing key.
    return str(error) if isinstance(error, brave.BraveError) else type(error).__name__


def _set_query(index: int, **fields) -> Callable[[WebSearchRun], None]:
    def change(run: WebSearchRun) -> None:
        queries = [dict(item) for item in run.queries]
        queries[index].update(fields)
        run.queries = queries

    return change


async def run_search(run_id: UUID, notify: Notify) -> str:
    """Run the search to the end; returns its final status."""
    from smarter_dev.web.web_search.queries import plan_queries
    from smarter_dev.web.web_search.ranking import rank

    run = _Run(run_id, notify)
    async with get_db_session_context() as session:
        row = await session.get(WebSearchRun, run_id)
        request = row.request

    def start(row: WebSearchRun) -> None:
        # A retried job starts over: every stage is cheap next to a stale half.
        row.status = "planning"
        row.queries = []
        row.results = []
        row.ranked = False
        row.error = None
        row.usage = {}
        row.attempt_count += 1
        row.started_at = datetime.now(UTC)
        row.finished_at = None

    await run.update(start)

    try:
        planned, luna_usage = await plan_queries(request)
    except Exception as error:  # noqa: BLE001 - shown to the user as a failed search
        # Type only, here and below: error text can echo the request or the
        # Brave URL, which carries the query.
        logger.error("Web search %s: Luna failed (%s)", run_id, type(error).__name__)
        return await _fail(run, f"Couldn't plan the searches ({type(error).__name__}).")
    await metering.record(run_id, lambda row: [metering.luna_row(row, luna_usage)])

    def searching(row: WebSearchRun) -> None:
        row.status = "searching"
        row.queries = [
            {**item, "status": "pending", "count": None, "domains": [], "hits": []}
            for item in planned
        ]
        row.usage = {**row.usage, "queries": luna_usage}

    await run.update(searching)

    answered = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for index, item in enumerate(planned):
            await run.update(_set_query(index, status="searching"))
            try:
                hits = await brave.search(client, item["query"])
            except Exception as error:  # noqa: BLE001 - one failed query is not fatal
                logger.warning(
                    "Web search %s: Brave failed on query %d (%s)", run_id, index, _brave_failure(error)
                )
                await run.update(_set_query(index, status="failed", count=0))
                continue
            answered += 1
            await run.update(
                _set_query(
                    index,
                    status="done",
                    count=len(hits),
                    domains=[hit["domain"] for hit in hits],
                    hits=hits,
                )
            )

    if answered:
        await metering.record(run_id, lambda row: [metering.brave_row(row, answered)])

    async with get_db_session_context() as session:
        row = await session.get(WebSearchRun, run_id)
        results = _merge(row.queries)
    if not results:
        return await _fail(run, "The searches found nothing.")

    def ranking(row: WebSearchRun) -> None:
        row.status = "ranking"
        row.results = results

    await run.update(ranking)

    try:
        judgments, jev_usage = await rank(request, results)
    except Exception as error:  # noqa: BLE001 - unranked results beat none
        logger.error("Web search %s: Jev failed (%s)", run_id, type(error).__name__)
        judgments, jev_usage = None, {"error": type(error).__name__}

    if judgments is not None:
        await metering.record(run_id, lambda row: [metering.jev_row(row, jev_usage)])
        ranked = [{**result, **judged} for result, judged in zip(results, judgments, strict=True)]
        ranked.sort(key=lambda item: (-item["score"], -item["best_probability"]))
    else:
        ranked = results

    def complete(row: WebSearchRun) -> None:
        row.status = "complete"
        row.results = ranked
        row.ranked = judgments is not None
        row.usage = {**row.usage, "ranking": jev_usage}
        row.finished_at = datetime.now(UTC)

    await run.update(complete)
    return "complete"


async def _fail(run: _Run, message: str) -> str:
    def fail(row: WebSearchRun) -> None:
        row.status = "error"
        row.error = message
        row.finished_at = datetime.now(UTC)

    await run.update(fail)
    return "error"
