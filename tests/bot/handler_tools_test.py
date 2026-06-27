"""Tests for the chatbot handler-management tools (mocked API + pipeline)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import smarter_dev.bot.agents.handler_tools as ht
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.handler_authoring import CreationResult


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeAPI:
    def __init__(self):
        self.get_responses = {}
        self.posted = []
        self.deleted = []
        self.post_response = _Resp(201, {"handler_id": "H1", "description": "d"})
        self.delete_response = _Resp(200, {"deleted": "H1"})
        self.list_response = _Resp(200, [])

    async def get(self, path, params=None):
        if path == "/handlers":
            return self.list_response
        return self.get_responses.get(path, _Resp(404))

    async def post(self, path, json_data=None):
        self.posted.append((path, json_data))
        return self.post_response

    async def delete(self, path):
        self.deleted.append(path)
        return self.delete_response


def _ctx(api):
    bot = SimpleNamespace(rest=SimpleNamespace())
    deps = ChatDeps(bot=bot, channel_id=111, guild_id=222, api_client=api)
    return SimpleNamespace(deps=deps)


async def _ok_pipeline(**kwargs):
    return CreationResult(ok=True, script='await send_message("hi")\n')


async def test_register_unknown_trigger(monkeypatch):
    api = _FakeAPI()
    out = await ht.register_handler(_ctx(api), "do a thing", "carrier pigeon")
    assert out.startswith("error: unknown trigger")


async def test_register_event_handler_happy(monkeypatch):
    monkeypatch.setattr(ht, "run_creation_pipeline", _ok_pipeline)
    api = _FakeAPI()
    out = await ht.register_handler(
        _ctx(api), "react on huzzah", "reaction add", {}, "999"
    )
    assert "Created handler H1" in out
    path, payload = api.posted[0]
    assert path == "/handlers"
    assert payload["trigger_type"] == "reaction"
    assert payload["channel_id"] == "999"


async def test_register_pipeline_error_relayed(monkeypatch):
    async def _err(**kwargs):
        return CreationResult(ok=False, error="cannot exceed 3 messages")

    monkeypatch.setattr(ht, "run_creation_pipeline", _err)
    api = _FakeAPI()
    out = await ht.register_handler(_ctx(api), "spam", "new message")
    assert out == "error: cannot exceed 3 messages"
    assert api.posted == []


async def test_register_schedule_below_floor(monkeypatch):
    monkeypatch.setattr(ht, "run_creation_pipeline", _ok_pipeline)
    api = _FakeAPI()
    out = await ht.register_handler(
        _ctx(api), "ping often", "schedule", {"interval_seconds": 5}
    )
    assert out.startswith("error:")
    assert api.posted == []


async def test_list_handlers_formats_rows():
    api = _FakeAPI()
    api.list_response = _Resp(
        200,
        [
            {"handler_id": "H1", "trigger_type": "message", "description": "a"},
            {"handler_id": "H2", "trigger_type": "timer", "description": "b"},
        ],
    )
    out = await ht.list_handlers(_ctx(api), "999")
    assert "H1" in out and "H2" in out and "[timer]" in out


async def test_list_handlers_empty():
    api = _FakeAPI()
    out = await ht.list_handlers(_ctx(api))
    assert "No handlers" in out


async def test_delete_handler():
    api = _FakeAPI()
    out = await ht.delete_handler(_ctx(api), "H1")
    assert "Deleted handler H1" in out
    assert api.deleted == ["/handlers/H1"]


async def test_delete_handler_not_found():
    api = _FakeAPI()
    api.delete_response = _Resp(404)
    out = await ht.delete_handler(_ctx(api), "ZZ")
    assert "No handler with id ZZ" in out
