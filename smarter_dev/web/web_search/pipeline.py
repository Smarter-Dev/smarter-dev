"""Run one dashboard web search, saving and announcing every stage.

Luna writes five queries, Brave runs each one, and Jev ranks the unique
results. When Luna judged that the request needs a written answer, the ranked
results go out first and Luna then reads pages and writes the answer. After each step the row is committed and then the owner is notified
with the whole snapshot, so the browser can render any event on its own and a
reload paints from the row.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
ANSWER_PUSH_SECONDS = 0.5
# The job's claim lasts 300 s; the answer must end well inside it.
ANSWER_TIMEOUT_SECONDS = 150


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
        row.needs_answer = False
        row.answer = None
        row.error = None
        row.usage = {}
        row.attempt_count += 1
        row.started_at = datetime.now(UTC)
        row.finished_at = None

    await run.update(start)

    try:
        planned, needs_answer, luna_usage = await plan_queries(request)
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
        row.needs_answer = needs_answer
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
        # The top pick leads, then the rest by score.
        ranked.sort(key=lambda item: (not item["best"], -item["score"], -item["best_probability"]))
    else:
        ranked = results

    def ranked_results(row: WebSearchRun) -> None:
        row.status = "answering" if needs_answer else "complete"
        row.results = ranked
        row.ranked = judgments is not None
        row.usage = {**row.usage, "ranking": jev_usage}
        if needs_answer:
            row.answer = {"status": "reading", "reads": [], "html": ""}
        else:
            row.finished_at = datetime.now(UTC)

    await run.update(ranked_results)
    if needs_answer:
        await _answer(run, request, ranked)
    return "complete"


async def _answer(run: _Run, request: str, results: list[dict]) -> None:
    """Write the answer above the ranked results, streaming it as it comes.

    A failed answer leaves the results in place with a short note."""
    from smarter_dev.web.web_search import answer as answering

    last_push = 0.0
    last_shape: tuple = ()

    def state(reads, text: str, status: str) -> dict:
        return {
            "status": status,
            "reads": [
                {"number": read.number, "domain": read.domain, "status": read.status}
                for read in reads
            ],
            "html": answering.render(text, results) if text else "",
        }

    async def progress(reads, text: str) -> None:
        nonlocal last_push, last_shape
        status = "writing" if text else "reading"
        shape = (status, [(read.number, read.status) for read in reads])
        now = time.monotonic()
        # Page reads and the switch to writing always go out; streamed text
        # at most every half second.
        if shape == last_shape and now - last_push < ANSWER_PUSH_SECONDS:
            return
        last_push, last_shape = now, shape
        current = state(reads, text, status)

        def change(row: WebSearchRun) -> None:
            row.answer = current

        await run.update(change)

    try:
        markdown, reads, usage = await asyncio.wait_for(
            answering.write_answer(request, results, progress),
            timeout=ANSWER_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - the results still stand without it
        logger.error("Web search %s: answer failed (%s)", run.run_id, type(error).__name__)

        def failed(row: WebSearchRun) -> None:
            row.status = "complete"
            row.answer = {
                **(row.answer or {}),
                "status": "failed",
                "error": "Couldn't write an answer this time. The results are below.",
            }
            row.finished_at = datetime.now(UTC)

        await run.update(failed)
        return

    await metering.record(run.run_id, lambda row: metering.answer_rows(row, usage))
    final = {**state(reads, markdown, "done"), "markdown": markdown}

    def done(row: WebSearchRun) -> None:
        row.status = "complete"
        row.answer = final
        row.usage = {**row.usage, "answer": usage}
        row.finished_at = datetime.now(UTC)

    await run.update(done)


async def _fail(run: _Run, message: str) -> str:
    def fail(row: WebSearchRun) -> None:
        row.status = "error"
        row.error = message
        row.finished_at = datetime.now(UTC)

    await run.update(fail)
    return "error"
