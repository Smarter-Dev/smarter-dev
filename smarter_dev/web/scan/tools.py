"""Standalone research tools for the Scan agent.

Provides Brave Search API, Jina Reader, and per-domain rate limiting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "Smarter Dev Scan Agent - admin@smarter.dev"


class RateLimiter:
    """Enforces a minimum delay between requests to the same key."""

    def __init__(self, min_delay: float = 5.0) -> None:
        self._min_delay = min_delay
        self._last_request: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def wait(self, key: str = "__global__") -> None:
        async with self._lock:
            if key in self._last_request:
                elapsed = (datetime.now() - self._last_request[key]).total_seconds()
                if elapsed < self._min_delay:
                    wait = self._min_delay - elapsed
                    logger.debug("Rate limit: waiting %.1fs for %s", wait, key)
                    await asyncio.sleep(wait)
            self._last_request[key] = datetime.now()


class URLRateLimiter(RateLimiter):
    """Per-domain rate limiter for URL requests."""

    def __init__(self, min_delay: float = 5.0) -> None:
        super().__init__(min_delay)

    async def wait_if_needed(self, url: str) -> None:
        domain = urlparse(url).netloc
        await self.wait(domain)


async def brave_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int = 5,
) -> list[dict]:
    """Search the web via the Brave Search API.

    Returns a list of dicts with keys: title, url, description.
    """
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not api_key:
        return [{"error": "BRAVE_SEARCH_API_KEY not configured"}]

    try:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(num_results, 20)},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            })
        return results

    except Exception as e:
        logger.error("Brave Search failed: %s", e)
        return [{"error": f"Search failed: {e}"}]


async def jina_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int = 5,
) -> list[dict]:
    """Search the web via the Jina Search API.

    Returns a list of dicts with keys: title, url, description, content.
    Unlike Brave Search, Jina returns page content with each result.
    """
    api_key = os.environ.get("JINA_API_KEY", "")
    headers: dict[str, str] = {
        "Accept": "application/json",
        "X-Retain-Images": "none",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = await client.get(
            f"https://s.jina.ai/{query}",
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code != 200:
            return [{"error": f"Jina Search returned {resp.status_code}"}]

        data = resp.json()
        results = []
        for item in data.get("data", [])[:num_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
                "content": item.get("content", ""),
            })
        return results

    except Exception as e:
        logger.error("Jina Search failed: %s", e)
        return [{"error": f"Search failed: {e}"}]


async def youtube_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int = 3,
) -> list[dict]:
    """Search YouTube via the Data API v3.

    Returns a list of dicts with keys: title, url, channel, thumbnail, video_id.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        return [{"error": "YOUTUBE_API_KEY not configured"}]

    try:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": min(num_results, 10),
                "relevanceLanguage": "en",
                "key": api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            results.append({
                "title": snippet.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "channel": snippet.get("channelTitle", ""),
                "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "video_id": video_id,
            })
        return results

    except Exception as e:
        logger.error("YouTube search failed: %s", e)
        return [{"error": f"YouTube search failed: {e}"}]


async def jina_read(client: httpx.AsyncClient, url: str) -> dict:
    """Read the full content of a URL via the Jina Reader API.

    Returns a dict with keys: title, description, content, url.
    """
    api_key = os.environ.get("JINA_API_KEY", "")
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = await client.get(
            f"https://r.jina.ai/{url}",
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code != 200:
            return {"error": f"Jina Reader returned {resp.status_code}", "url": url}

        try:
            data = resp.json().get("data", {})
        except Exception:
            # Jina sometimes returns non-JSON (e.g. for blocked sites)
            text = resp.text[:3000] if resp.text else ""
            if text:
                return {
                    "title": "",
                    "description": "",
                    "content": text,
                    "url": url,
                }
            return {"error": "Jina Reader returned non-JSON response", "url": url}

        return {
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "content": data.get("content", ""),
            "url": data.get("url", url),
        }

    except Exception as e:
        logger.error("Jina Reader failed for %s: %s (%s)", url, e, type(e).__name__)
        return {"error": f"Failed to read URL ({type(e).__name__}): {e}", "url": url}
