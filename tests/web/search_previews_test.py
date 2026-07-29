"""Tests for short-lived, read-only agent search-result previews."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from litestar.exceptions import NotFoundException
from sqlalchemy import select

from smarter_dev.web.models import SearchResultPreview
from smarter_dev.web.search_preview_controller import search_preview_view
from smarter_dev.web.search_previews import SEARCH_PREVIEW_RETENTION
from smarter_dev.web.search_previews import complete_search_preview
from smarter_dev.web.search_previews import create_search_preview
from smarter_dev.web.search_previews import delete_expired_search_previews
from smarter_dev.web.search_previews import fail_search_preview
from smarter_dev.web.search_previews import get_active_search_preview

_TOKEN = "test-capability-token"
_NOW = datetime(2026, 7, 29, 21, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_preview_is_reserved_pending_then_populated(db_session):
    reservation = await create_search_preview(
        db_session,
        "python async task groups",
        now=_NOW,
        token=_TOKEN,
        site_base_url="https://smarter.dev/",
    )
    await db_session.commit()

    assert reservation.url == f"https://smarter.dev/ai/search/{_TOKEN}"
    preview = await get_active_search_preview(
        db_session, _TOKEN, now=_NOW + timedelta(seconds=1)
    )
    assert preview is not None
    assert preview.status == "pending"
    assert preview.results == []
    assert preview.expires_at.replace(tzinfo=UTC) == _NOW + SEARCH_PREVIEW_RETENTION
    assert preview.access_token_hash != _TOKEN

    results = [
        {
            "title": "Task Groups",
            "url": "https://docs.python.org/3/library/asyncio-task.html",
            "description": "Structured concurrency documentation.",
            "ignored_provider_field": "not shown to the agent",
        }
    ]
    await complete_search_preview(db_session, reservation.id, results)
    await db_session.commit()

    completed = await db_session.scalar(
        select(SearchResultPreview).where(SearchResultPreview.id == reservation.id)
    )
    assert completed is not None
    assert completed.status == "ready"
    assert completed.results == [
        {
            "title": "Task Groups",
            "url": "https://docs.python.org/3/library/asyncio-task.html",
            "description": "Structured concurrency documentation.",
        }
    ]


@pytest.mark.asyncio
async def test_expired_preview_is_inaccessible_before_cleanup(db_session):
    await create_search_preview(
        db_session,
        "expired query",
        now=_NOW,
        token=_TOKEN,
        site_base_url="https://smarter.dev",
    )
    await db_session.commit()

    assert (
        await get_active_search_preview(
            db_session, _TOKEN, now=_NOW + SEARCH_PREVIEW_RETENTION
        )
        is None
    )
    assert await db_session.scalar(select(SearchResultPreview)) is not None


@pytest.mark.asyncio
async def test_cleanup_deletes_only_expired_previews(db_session):
    await create_search_preview(
        db_session,
        "drop",
        now=_NOW - SEARCH_PREVIEW_RETENTION,
        token="expired-token",
        site_base_url="https://smarter.dev",
    )
    keep = await create_search_preview(
        db_session,
        "keep",
        now=_NOW,
        token="active-token",
        site_base_url="https://smarter.dev",
    )
    await db_session.commit()

    assert await delete_expired_search_previews(db_session, now=_NOW) == 1
    await db_session.commit()
    rows = list((await db_session.scalars(select(SearchResultPreview))).all())
    assert [row.id for row in rows] == [keep.id]


@pytest.mark.asyncio
async def test_failed_preview_publishes_no_error_details(db_session):
    reservation = await create_search_preview(
        db_session,
        "failed",
        now=_NOW,
        token=_TOKEN,
        site_base_url="https://smarter.dev",
    )
    await fail_search_preview(db_session, reservation.id)
    await db_session.commit()

    preview = await get_active_search_preview(db_session, _TOKEN, now=_NOW)
    assert preview is not None
    assert preview.status == "failed"
    assert preview.results == []


@pytest.mark.asyncio
async def test_view_is_read_only_and_rejects_unsafe_result_links(db_session):
    reservation = await create_search_preview(
        db_session,
        "render this",
        now=datetime.now(UTC),
        token=_TOKEN,
        site_base_url="https://smarter.dev",
    )
    await complete_search_preview(
        db_session,
        reservation.id,
        [
            {
                "title": "Unsafe",
                "url": "javascript:alert(1)",
                "description": "Still rendered as inert text.",
            }
        ],
    )
    await db_session.commit()

    response = await search_preview_view.fn(_TOKEN, db_session)
    assert response.template_name == "ai/search_preview.html"
    assert response.context["status"] == "ready"
    assert response.context["results"][0]["safe_url"] is None
    assert response.headers["Cache-Control"] == "no-store"
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_view_returns_not_found_for_unknown_token(db_session):
    with pytest.raises(NotFoundException):
        await search_preview_view.fn("unknown", db_session)
