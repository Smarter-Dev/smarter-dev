"""Bot-authenticated REST persistence for web-search preview capabilities."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import post
from litestar import put
from litestar.status_codes import HTTP_200_OK
from litestar.status_codes import HTTP_201_CREATED
from pydantic import BaseModel
from pydantic import Field
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.search_previews import complete_search_preview
from smarter_dev.web.search_previews import create_search_preview
from smarter_dev.web.search_previews import fail_search_preview

BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission("bot-api")]


class SearchPreviewCreate(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)


class SearchPreviewReservationRead(BaseModel):
    id: UUID
    url: str


class SearchPreviewResults(BaseModel):
    results: list[dict[str, Any]] = Field(max_length=20)


class SearchPreviewPersistenceController(Controller):
    path = "/api/search-previews"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post(status_code=HTTP_201_CREATED, guards=BOT_API_GUARDS)
    async def reserve(
        self, db_session: AsyncSession, data: SearchPreviewCreate
    ) -> SearchPreviewReservationRead:
        reservation = await create_search_preview(db_session, data.query)
        await db_session.commit()
        return SearchPreviewReservationRead(id=reservation.id, url=reservation.url)

    @put("/{preview_id:uuid}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def complete(
        self,
        db_session: AsyncSession,
        preview_id: UUID,
        data: SearchPreviewResults,
    ) -> dict[str, bool]:
        await complete_search_preview(db_session, preview_id, data.results)
        await db_session.commit()
        return {"updated": True}

    @post(
        "/{preview_id:uuid}/failed",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def fail(self, db_session: AsyncSession, preview_id: UUID) -> dict[str, bool]:
        await fail_search_preview(db_session, preview_id)
        await db_session.commit()
        return {"updated": True}
