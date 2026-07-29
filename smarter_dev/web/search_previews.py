"""Create and expire read-only previews of agent web-search results.

A preview is a capability URL, not a search endpoint. It is reserved before the
provider request so the initial Discord tool-use message can link to it, then
completed in a separate transaction after the provider returns.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import SearchResultPreview

SEARCH_PREVIEW_RETENTION = timedelta(hours=48)
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class SearchPreviewReservation:
    """The private handle and public URL returned when a preview is reserved."""

    id: UUID
    url: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalise_results(results: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep the ordered title/URL/description fields shown to the agent."""
    normalised: list[dict[str, str]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        normalised.append(
            {
                "title": str(result.get("title", "") or ""),
                "url": str(result.get("url", "") or ""),
                "description": str(result.get("description", "") or ""),
            }
        )
    return normalised


async def create_search_preview(
    session: AsyncSession,
    query: str,
    *,
    now: datetime | None = None,
    token: str | None = None,
    site_base_url: str | None = None,
) -> SearchPreviewReservation:
    """Reserve a pending preview and return its unguessable public URL.

    The caller owns the transaction. ``token`` and ``now`` are injectable for
    deterministic tests; production callers should omit both.
    """
    now = now or datetime.now(UTC)
    raw_token = token or secrets.token_urlsafe(_TOKEN_BYTES)
    row = SearchResultPreview(
        access_token_hash=_hash_token(raw_token),
        query=query,
        status="pending",
        results=[],
        expires_at=now + SEARCH_PREVIEW_RETENTION,
    )
    session.add(row)
    await session.flush()

    base_url = (site_base_url or get_settings().site_base_url).rstrip("/")
    return SearchPreviewReservation(
        id=row.id,
        url=f"{base_url}/ai/search/{raw_token}",
    )


async def complete_search_preview(
    session: AsyncSession,
    preview_id: UUID,
    results: Sequence[dict[str, Any]],
) -> None:
    """Populate a reserved preview without changing its query or expiry."""
    await session.execute(
        update(SearchResultPreview)
        .where(
            SearchResultPreview.id == preview_id,
            SearchResultPreview.status == "pending",
        )
        .values(status="ready", results=_normalise_results(results))
    )


async def fail_search_preview(session: AsyncSession, preview_id: UUID) -> None:
    """Mark a reserved preview failed without publishing provider details."""
    await session.execute(
        update(SearchResultPreview)
        .where(
            SearchResultPreview.id == preview_id,
            SearchResultPreview.status == "pending",
        )
        .values(status="failed", results=[])
    )


async def get_active_search_preview(
    session: AsyncSession,
    token: str,
    *,
    now: datetime | None = None,
) -> SearchResultPreview | None:
    """Resolve a capability token, excluding expired previews immediately."""
    if not token or len(token) > 128:
        return None
    now = now or datetime.now(UTC)
    return await session.scalar(
        select(SearchResultPreview).where(
            SearchResultPreview.access_token_hash == _hash_token(token),
            SearchResultPreview.expires_at > now,
        )
    )


async def delete_expired_search_previews(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """Physically delete previews whose public lifetime has ended."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        delete(SearchResultPreview).where(SearchResultPreview.expires_at <= now)
    )
    return result.rowcount or 0


async def reserve_search_preview(query: str) -> SearchPreviewReservation:
    """Reserve and commit a preview using a short independent transaction."""
    async with get_db_session_context() as session:
        reservation = await create_search_preview(session, query)
        await session.commit()
        return reservation


async def populate_search_preview(
    preview_id: UUID, results: Sequence[dict[str, Any]]
) -> None:
    """Complete and commit a preview using a short independent transaction."""
    async with get_db_session_context() as session:
        await complete_search_preview(session, preview_id, results)
        await session.commit()


async def mark_search_preview_failed(preview_id: UUID) -> None:
    """Fail and commit a preview using a short independent transaction."""
    async with get_db_session_context() as session:
        await fail_search_preview(session, preview_id)
        await session.commit()
