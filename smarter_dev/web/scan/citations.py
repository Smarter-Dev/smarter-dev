"""Inline citation processing for research responses.

Converts [[https://example.com/page]] markers into superscripted domain
pill links in HTML output.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_CITATION_RE = re.compile(r"\[\[(https?://[^\]\s]+)\]\]")


def _domain(url: str) -> str:
    """Extract a short display domain from a URL."""
    host = urlparse(url).hostname or url
    # Strip www. prefix
    if host.startswith("www."):
        host = host[4:]
    return host


def process_citations(html: str) -> str:
    """Replace [[url]] markers in HTML with superscripted domain pill links.

    Works on already-rendered HTML — the [[url]] markers survive markdown
    rendering as literal text.
    """
    def _replace(m: re.Match) -> str:
        url = m.group(1)
        domain = _domain(url)
        return (
            f'<sup><a href="{url}" target="_blank" rel="noopener" '
            f'class="scan-cite">{domain}</a></sup>'
        )

    return _CITATION_RE.sub(_replace, html)
