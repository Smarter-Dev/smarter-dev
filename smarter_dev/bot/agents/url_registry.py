"""Registry of URLs we XML-escaped when rendering them to the chat agent.

Attachment URLs are rendered into ``<attachment url="…">`` XML attributes, which
escapes ``&`` -> ``&amp;`` (etc.). The model copies the escaped form back into
``web_read``, which would break signed-URL query params (e.g. Discord CDN's
ex/is/hm) if fetched as-is.

We can't just un-escape any URL with ``&amp;`` in it — URLs from web search or
users may legitimately contain that substring, and reversing it would corrupt
them. So instead we record every URL *we* escape (escaped form -> original) and
only resolve a read URL back when it's one we actually produced. Unknown URLs
pass through untouched.

The store is bounded and warmed on every render, so a URL stays resolvable for
as long as its message is in the agent's context.
"""

from __future__ import annotations

from collections import OrderedDict
from xml.sax.saxutils import escape as xml_escape

# Cap on tracked URLs. Each render re-registers (and refreshes) the URLs still in
# context, so eviction only drops URLs that have scrolled out of the agent's view.
_MAX_TRACKED = 1024

_escaped_to_original: OrderedDict[str, str] = OrderedDict()


def _xml_escape_attr(value: str) -> str:
    """Escape a string exactly as ``chat_input_format._attr`` does for an attribute."""
    return xml_escape(value, {'"': "&quot;"})


def register_escaped_url(url: str) -> None:
    """Record ``url`` so a later ``web_read`` of its escaped form resolves back.

    No-op when escaping changes nothing (no special characters), since there's
    then no escaped/original ambiguity to resolve.
    """
    escaped = _xml_escape_attr(url)
    if escaped == url:
        return
    _escaped_to_original[escaped] = url
    _escaped_to_original.move_to_end(escaped)
    while len(_escaped_to_original) > _MAX_TRACKED:
        _escaped_to_original.popitem(last=False)


def resolve_escaped_url(url: str) -> str:
    """Return the original URL if ``url`` is an escaped form we produced, else ``url``."""
    return _escaped_to_original.get(url, url)
