"""What the browser sees of a web search, from the page load and every event.

The web pod imports this module, so it must not import pydantic-ai."""

from __future__ import annotations

from smarter_dev.web.models import WebSearchRun

ACTIVE_STATUSES = ("queued", "planning", "searching", "ranking")
# The notification type the dashboard listens for.
EVENT_TYPE = "web_search"
MAX_REQUEST_CHARS = 500


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot(run: WebSearchRun) -> dict:
    """The whole search, as the dashboard renders it."""
    return {
        "id": str(run.id),
        "request": run.request,
        "status": run.status,
        "active": run.status in ACTIVE_STATUSES,
        "queries": [
            {
                "query": item.get("query", ""),
                "angle": item.get("angle", ""),
                "status": item.get("status", "pending"),
                "count": item.get("count"),
                "domains": item.get("domains", []),
            }
            for item in run.queries or []
        ],
        "results": run.results or [],
        "ranked": run.ranked,
        "error": run.error,
        "version": run.version,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    }


def summary(run: WebSearchRun) -> dict:
    """One row of the recent searches list."""
    results = run.results or []
    return {
        "id": str(run.id),
        "request": run.request,
        "status": run.status,
        "active": run.status in ACTIVE_STATUSES,
        "relevant": sum(1 for item in results if item.get("relevant")),
        "created_at": _iso(run.created_at),
    }
