from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native import search_previews as module
from smarter_dev.web.api_native.search_previews import (
    SearchPreviewPersistenceController,
)


def test_search_preview_persistence_routes(monkeypatch):
    preview_id = uuid4()
    session = AsyncMock(spec=AsyncSession)
    reserve = AsyncMock(
        return_value=SimpleNamespace(
            id=preview_id, url="https://smarter.dev/ai/search/capability"
        )
    )
    complete = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(module, "create_search_preview", reserve)
    monkeypatch.setattr(module, "complete_search_preview", complete)
    monkeypatch.setattr(module, "fail_search_preview", fail)
    original_guards = list(module.BOT_API_GUARDS)
    module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[SearchPreviewPersistenceController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(lambda: session, sync_to_thread=False)},
        ) as client:
            response = client.post("/api/search-previews", json={"query": "redis"})
            assert response.status_code == 201
            assert response.json() == {
                "id": str(preview_id),
                "url": "https://smarter.dev/ai/search/capability",
            }

            response = client.put(
                f"/api/search-previews/{preview_id}",
                json={"results": [{"title": "Redis", "url": "https://redis.io"}]},
            )
            assert response.status_code == 200

            response = client.post(f"/api/search-previews/{preview_id}/failed")
            assert response.status_code == 200
    finally:
        module.BOT_API_GUARDS[:] = original_guards

    reserve.assert_awaited_once_with(session, "redis")
    complete.assert_awaited_once()
    fail.assert_awaited_once_with(session, preview_id)
    assert session.commit.await_count == 3
