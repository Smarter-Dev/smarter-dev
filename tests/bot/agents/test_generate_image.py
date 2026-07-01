"""Tests for the chat agent's ``generate_image`` tool.

The tool orchestrates: quota peek → policy review → reserve → generate →
attach, with a refund when generation fails. These tests inject a fake API
client (via ``ChatDeps.api_client``) and stub the reviewer + image model so the
decision logic is exercised without touching Redis or Gemini.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import smarter_dev.bot.agents.chat_tools as chat_tools
from smarter_dev.bot.agents.chat_tools import ChatDeps, generate_image
from smarter_dev.bot.agents.image_prompt_reviewer import ImagePromptDecision


class _Resp:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status_code = status

    def json(self) -> dict:
        return self._data


class _FakeAPI:
    """Fake APIClient for the image-quota endpoints, recording calls."""

    def __init__(self, quota: dict, reserve: dict | None = None):
        self._quota = quota
        self._reserve = reserve or {}
        self.calls: list[tuple] = []
        self.released = False

    async def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return _Resp(self._quota)

    async def post(self, path, json_data=None):
        self.calls.append(("POST", path, json_data))
        if path.endswith("/reserve"):
            return _Resp(self._reserve)
        if path.endswith("/release"):
            self.released = True
            return _Resp({"released": (json_data or {}).get("guild_id")})
        return _Resp({})


def _ctx(api: _FakeAPI) -> SimpleNamespace:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return SimpleNamespace(
        deps=ChatDeps(bot=bot, channel_id=1, guild_id=99, api_client=api)
    )


def _reserved_count(api: _FakeAPI) -> int:
    return sum(1 for m, p, _ in api.calls if m == "POST" and p.endswith("/reserve"))


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    """Default: reviewer approves, model returns a PNG. Individual tests override."""
    monkeypatch.setattr(
        chat_tools,
        "review_image_prompt",
        AsyncMock(return_value=ImagePromptDecision(approved=True, reason="ok")),
    )
    monkeypatch.setattr(
        chat_tools,
        "generate_image_bytes",
        AsyncMock(return_value=(b"PNGDATA", "image/png")),
    )


async def test_exhausted_quota_skips_review_and_generation(monkeypatch):
    review = AsyncMock()
    monkeypatch.setattr(chat_tools, "review_image_prompt", review)
    api = _FakeAPI(quota={"remaining": 0, "limit": 5, "resets_at": "2026-07-01T13:00Z",
                          "retry_after_seconds": 1800})
    ctx = _ctx(api)

    out = await generate_image(ctx, "a binary tree diagram")

    assert "No image generated" in out
    assert "do NOT call generate_image again" in out
    assert "2026-07-01T13:00Z" in out
    review.assert_not_awaited()
    assert _reserved_count(api) == 0
    assert ctx.deps.pending_images == []


async def test_rejected_prompt_returns_reason_without_spending(monkeypatch):
    monkeypatch.setattr(
        chat_tools,
        "review_image_prompt",
        AsyncMock(return_value=ImagePromptDecision(
            approved=False, reason="That's an off-topic picture, not a technical diagram."
        )),
    )
    api = _FakeAPI(quota={"remaining": 3, "limit": 5, "resets_at": None})
    ctx = _ctx(api)

    out = await generate_image(ctx, "a sunset over the ocean")

    assert "rejected" in out.lower()
    assert "off-topic picture" in out
    assert "no quota spent" in out.lower()
    assert _reserved_count(api) == 0
    assert ctx.deps.pending_images == []


async def test_approved_prompt_reserves_and_attaches_image():
    api = _FakeAPI(
        quota={"remaining": 5, "limit": 5, "resets_at": None},
        reserve={"granted": True, "remaining": 4, "limit": 5,
                 "resets_at": "2026-07-01T13:00Z"},
    )
    ctx = _ctx(api)

    out = await generate_image(ctx, "diagram of a hash table with chaining")

    assert _reserved_count(api) == 1
    assert len(ctx.deps.pending_images) == 1
    img = ctx.deps.pending_images[0]
    assert img.data == b"PNGDATA"
    assert img.mime_type == "image/png"
    assert img.filename == "diagram.png"
    assert "attached to your reply" in out
    assert "4 of 5" in out
    assert not api.released


async def test_reserve_denied_race_returns_no_image():
    api = _FakeAPI(
        quota={"remaining": 1, "limit": 5, "resets_at": None},
        reserve={"granted": False, "remaining": 0, "limit": 5,
                 "resets_at": "2026-07-01T13:00Z", "retry_after_seconds": 600},
    )
    ctx = _ctx(api)

    out = await generate_image(ctx, "a red-black tree")

    assert "No image generated" in out
    assert ctx.deps.pending_images == []


async def test_generation_failure_refunds_the_slot(monkeypatch):
    monkeypatch.setattr(
        chat_tools,
        "generate_image_bytes",
        AsyncMock(side_effect=RuntimeError("model boom")),
    )
    api = _FakeAPI(
        quota={"remaining": 2, "limit": 5, "resets_at": None},
        reserve={"granted": True, "remaining": 1, "limit": 5, "resets_at": None},
    )
    ctx = _ctx(api)

    out = await generate_image(ctx, "UML class diagram")

    assert "failed" in out.lower()
    assert "refunded" in out.lower()
    assert api.released is True
    assert ctx.deps.pending_images == []
