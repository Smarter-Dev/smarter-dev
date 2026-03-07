"""Request/response models for the Scan research API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    user_id: str
    guild_id: str | None = None
    channel_id: str | None = None
    context: dict | None = None


class ResearchResponse(BaseModel):
    session_id: UUID
    source_key: str
    stream_url: str
    status: str = "running"


class FollowupRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class SourceSchema(BaseModel):
    url: str
    title: str
    type: str = "other"
    snippet: str = ""
    cited: bool = False


class ResearchSessionSchema(BaseModel):
    id: UUID
    query: str
    name: str | None = None
    user_id: str
    guild_id: str | None = None
    channel_id: str | None = None
    status: str
    response: str | None = None
    summary: str | None = None
    sources: list[SourceSchema] = []
    followups: list[dict] = []
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ResearchEvent(BaseModel):
    """An SSE event emitted during research."""
    event: str
    data: dict
