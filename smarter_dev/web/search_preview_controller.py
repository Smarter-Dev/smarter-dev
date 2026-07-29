"""Public, read-only views of short-lived agent web-search snapshots."""

from __future__ import annotations

from urllib.parse import urlparse

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.search_previews import get_active_search_preview

_PREVIEW_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
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


@get("/ai/search/{token:str}")
async def search_preview_view(
    token: str, db_session: AsyncSession
) -> Template:
    preview = await get_active_search_preview(db_session, token)
    if preview is None:
        raise NotFoundException()

    results = [
        {
            "title": str(result.get("title", "") or ""),
            "url": str(result.get("url", "") or ""),
            "safe_url": _safe_external_url(str(result.get("url", "") or "")),
            "description": str(result.get("description", "") or ""),
        }
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
        },
        headers=_PREVIEW_HEADERS,
    )
