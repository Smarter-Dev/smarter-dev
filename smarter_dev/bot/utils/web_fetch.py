"""Web fetch utilities used by the chat agent's `web_read` tool.

Pulled out of the legacy `agents/tools.py` so the helpers remain
test-importable while the old agent module gets rewritten.
"""

from __future__ import annotations

import io
import logging
import os
import re
from urllib.parse import urlparse

import httpx
import pdfplumber

logger = logging.getLogger(__name__)

USER_AGENT = "Smarter Dev Discord Bot - admin@smarter.dev"

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def is_youtube_url(url: str) -> bool:
    """Return True if the URL points at a YouTube video page."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return hostname in ("www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com")


async def fetch_via_jina(url: str) -> dict[str, str] | None:
    """Fetch a URL via Jina Reader, returning markdown content.

    Returns dict with keys ``title``, ``description``, ``content``, ``url``
    on success, or ``None`` on failure.
    """
    api_key = os.environ.get("JINA_API_KEY", "")
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)
            if resp.status_code != 200:
                logger.debug("Jina Reader returned %s for %s", resp.status_code, url)
                return None
            data = resp.json().get("data", {})
            return {
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "content": data.get("content", ""),
                "url": data.get("url", url),
            }
    except Exception as e:
        logger.debug("Jina Reader failed for %s: %s", url, e)
        return None


async def fetch_youtube_metadata(url: str) -> dict[str, str]:
    """Fetch YouTube video metadata via Jina Reader + oEmbed.

    Returns a dict with optional ``title``, ``author``, ``description`` keys.
    """
    metadata: dict[str, str] = {}
    try:
        jina_data = await fetch_via_jina(url)
        if jina_data:
            if jina_data["title"]:
                metadata["title"] = jina_data["title"]
            if jina_data["description"]:
                metadata["description"] = jina_data["description"]

        async with httpx.AsyncClient(timeout=10.0) as client:
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            resp = await client.get(oembed_url)
            if resp.status_code == 200:
                data = resp.json()
                metadata["author"] = data.get("author_name", "")
                if "title" not in metadata and data.get("title"):
                    metadata["title"] = data["title"]
    except Exception as e:
        logger.debug("Could not fetch YouTube metadata for %s: %s", url, e)
    return metadata


async def fetch_pdf_text(url: str, max_chars: int = 20_000) -> str | None:
    """Download a PDF and extract plain text via pdfplumber.

    Returns the extracted text (truncated to ``max_chars``) or ``None`` on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200:
                return None
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                pages = []
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    pages.append(text)
                    if sum(len(p) for p in pages) >= max_chars:
                        break
            return "\n\n".join(pages)[:max_chars]
    except Exception as e:
        logger.debug("PDF fetch failed for %s: %s", url, e)
        return None


def strip_html(html: str) -> str:
    """Fallback HTML→text used only when Jina is unavailable."""
    cleaned = _SCRIPT_STYLE_RE.sub("", html)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


__all__ = [
    "USER_AGENT",
    "_HTML_TAG_RE",
    "_SCRIPT_STYLE_RE",
    "fetch_pdf_text",
    "fetch_via_jina",
    "fetch_youtube_metadata",
    "is_youtube_url",
    "strip_html",
]
