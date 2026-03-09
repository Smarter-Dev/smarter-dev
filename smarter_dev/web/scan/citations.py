"""Inline citation processing for research responses.

Converts [[https://example.com/page]] markers into superscripted domain
pill links in HTML output.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s,\]\[]+")

# [[url]] or [[url], [url], [url]] — allow inner brackets for comma-separated lists
_CITATION_RE = re.compile(r"\[\[(https?://[^\]\s]+(?:],\s*\[https?://[^\]\s]+)*)\]\]")


def _domain(url: str) -> str:
    """Extract a short display domain from a URL."""
    host = urlparse(url).hostname or url
    if host.startswith("www."):
        host = host[4:]
    return host


def _cite(url: str) -> str:
    return (
        f'<sup><a href="{url}" target="_blank" rel="noopener" '
        f'class="scan-cite">{_domain(url)}</a></sup>'
    )


def process_citations(html: str) -> str:
    """Replace [[url]] markers in HTML with superscripted domain pill links.

    Handles both ``[[url]]`` and ``[[url], [url], [url]]`` formats.
    Works on already-rendered HTML — the markers survive markdown rendering.
    """
    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        urls = _URL_RE.findall(inner)
        if not urls:
            return m.group(0)
        return "".join(_cite(url) for url in urls)

    return _CITATION_RE.sub(_replace, html)
