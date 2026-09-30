"""Brave web search for the dashboard, paced to the plan's rate limit."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_HOST = urlparse(BRAVE_URL).hostname


class _HideBraveRequests(logging.Filter):
    """Drop httpx's request lines for Brave: their URL carries the query,
    which Luna wrote from the user's request. The worker keeps httpx at INFO
    for everything else."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            getattr(arg, "host", None) == BRAVE_HOST for arg in record.args or ()
        )


logging.getLogger("httpx").addFilter(_HideBraveRequests())
RESULTS_PER_QUERY = 5
# Brave's base plan allows 1 request a second. The pacing is per worker
# process, so the other Brave users in the agent-worker (the research agents)
# can still collide with it; one retry covers that.
MIN_INTERVAL_SECONDS = float(os.getenv("BRAVE_MIN_INTERVAL_SECONDS", "1.1"))
RATE_LIMIT_RETRY_SECONDS = 1.5
# What one answered request costs, for the usage ledger: Brave's published
# $5 per 1,000 requests. Set BRAVE_PRICE_PER_REQUEST_USD if the plan differs.
PRICE_PER_REQUEST_USD = os.getenv("BRAVE_PRICE_PER_REQUEST_USD", "0.005")

_lock = asyncio.Lock()
_last_call = 0.0


class BraveError(RuntimeError):
    pass


def clean_snippet(text: str) -> str:
    """Brave snippets carry <strong> highlight tags and HTML entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def domain_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.")


async def _paced_get(client: httpx.AsyncClient, query: str, count: int) -> httpx.Response:
    global _last_call
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not api_key:
        raise BraveError("BRAVE_SEARCH_API_KEY is not configured")
    async with _lock:
        wait = _last_call + MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await client.get(
                BRAVE_URL,
                params={"q": query, "count": count},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
            )
        finally:
            _last_call = time.monotonic()


async def search(
    client: httpx.AsyncClient, query: str, count: int = RESULTS_PER_QUERY
) -> list[dict]:
    """Up to ``count`` results as ``{"title", "url", "domain", "snippet",
    "favicon", "age"}``; the last two are empty when Brave has none."""
    response = await _paced_get(client, query, count)
    if response.status_code == 429:
        await asyncio.sleep(RATE_LIMIT_RETRY_SECONDS)
        response = await _paced_get(client, query, count)
    if response.status_code != 200:
        raise BraveError(f"Brave answered {response.status_code}")
    results = []
    for item in response.json().get("web", {}).get("results", [])[:count]:
        url = item.get("url", "")
        if not url.startswith(("https://", "http://")):
            continue
        # Brave proxies favicons through its own image host.
        favicon = (item.get("meta_url") or {}).get("favicon", "")
        results.append(
            {
                "title": clean_snippet(item.get("title", "")),
                "url": url,
                "domain": domain_of(url),
                "snippet": clean_snippet(item.get("description", "")),
                "favicon": favicon if favicon.startswith("https://") else "",
                "age": item.get("age", "") or "",
            }
        )
    return results
