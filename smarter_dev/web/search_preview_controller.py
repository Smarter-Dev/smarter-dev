"""Public, read-only views of short-lived agent web-search snapshots."""

from __future__ import annotations

from html import unescape
from urllib.parse import urlparse

import nh3
from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.web.search_previews import get_active_search_preview

_PREVIEW_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
}


def _safe_external_url(url: str) -> str | None:
    """Return only absolute HTTP(S) links; untrusted text still renders."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _plain_text(value: str) -> str:
    """Strip all markup from a title, then decode its character entities."""
    return unescape(nh3.clean(value, tags=set(), attributes={}))


def _snippet_html(value: str) -> str:
    """Keep only harmless emphasis markup from Brave's result snippet."""
    return nh3.clean(
        value,
        tags={"strong", "em"},
        attributes={},
        strip_comments=True,
    )


def _result_view(result: dict) -> dict[str, str | None]:
    url = str(result.get("url", "") or "")
    safe_url = _safe_external_url(url)
    parsed = urlparse(safe_url) if safe_url else None
    domain = (parsed.hostname or "") if parsed else ""
    if domain.startswith("www."):
        domain = domain[4:]
    display_url = ""
    if parsed:
        display_url = parsed.path.rstrip("/")
        if parsed.query:
            display_url += f"?{parsed.query}"

    return {
        "title": _plain_text(str(result.get("title", "") or "")),
        "url": url,
        "safe_url": safe_url,
        "domain": domain,
        "display_url": display_url,
        "description_html": _snippet_html(
            str(result.get("description", "") or "")
        ),
    }


@get("/ai/search/{token:str}")
async def search_preview_view(
    token: str, db_session: AsyncSession
) -> Template:
    preview = await get_active_search_preview(db_session, token)
    if preview is None:
        raise NotFoundException()

    results = [
        _result_view(result)
        for result in (preview.results or [])
        if isinstance(result, dict)
    ]

    return Template(
        "ai/search_preview.html",
        context={
            "query": preview.query,
            "status": preview.status,
            "results": results,
            "created_at": preview.created_at,
            "expires_at": preview.expires_at,
            "disable_analytics": True,
            "config": get_settings(),
        },
        headers=_PREVIEW_HEADERS,
    )
