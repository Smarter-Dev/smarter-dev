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
        self.updated = []
        self.deleted = []
        self.post_response = _Resp(
            201, {"handler_id": "H1", "name": "greeter", "description": "d"}
        )
        self.put_response = _Resp(
            200, {"handler_id": "H1", "name": "greeter", "description": "d"}
        )
        self.delete_response = _Resp(200, {"deleted": "H1"})
        self.list_response = _Resp(200, [])

    async def get(self, path, params=None):
        if path == "/handlers":
            return self.list_response
        return self.get_responses.get(path, _Resp(404))

    async def post(self, path, json_data=None):
        self.posted.append((path, json_data))
        return self.post_response

    async def put(self, path, json_data=None):
        self.updated.append((path, json_data))
        return self.put_response

    async def delete(self, path):
        self.deleted.append(path)
        return self.delete_response


def _ctx(api):
    bot = SimpleNamespace(rest=SimpleNamespace())
    deps = ChatDeps(bot=bot, channel_id=111, guild_id=222, api_client=api)
    return SimpleNamespace(deps=deps)


def _create_result(**over):
    fields = {
        "ok": True,
        "action": "create",
        "target_handler_id": None,
        "name": "hi-sayer",
        "trigger_type": "message",
        "settings": {},
        "description": "says hi",
        "script": 'await send_message("hi")\n',
    }
    fields.update(over)
    return CreationResult(**fields)


async def test_register_unknown_trigger(monkeypatch):
    api = _FakeAPI()
    out = await ht.register_handler(_ctx(api), "do a thing", "carrier pigeon")
    assert out.startswith("error: unknown trigger")


async def test_register_creates_named_handler(monkeypatch):
    seen = {}

    async def _pipeline(**kwargs):
        seen.update(kwargs)
        return _create_result()

    monkeypatch.setattr(ht, "run_creation_pipeline", _pipeline)
    api = _FakeAPI()
    api.post_response = _Resp(
        201, {"handler_id": "H1", "name": "hi-sayer", "description": "says hi"}
    )
    api.list_response = _Resp(
        200,
        [
            {
                "handler_id": "H9",
                "name": "old-greeter",
                "trigger_type": "message",
                "settings": {},
                "description": "greets",
                "script": "pass\n",
            }
        ],
    )
    out = await ht.register_handler(
        _ctx(api), "react on huzzah", "reaction add", {}, "999"
    )
    assert "hi-sayer" in out
    # The pipeline saw the channel's existing handlers, scripts included.
    assert seen["existing_handlers"][0]["name"] == "old-greeter"
    assert seen["existing_handlers"][0]["script"] == "pass\n"
    path, payload = api.posted[0]
    assert path == "/handlers"
    assert payload["name"] == "hi-sayer"
    assert payload["trigger_type"] == "message"  # the plan's trigger, not the hint
    assert payload["channel_id"] == "999"


async def test_register_edits_existing_handler(monkeypatch):
    async def _pipeline(**kwargs):
        return _create_result(
            action="edit",
            target_handler_id="H9",
            name="old-greeter",
            description="greets adventurers",
            script='await send_message("hail")\n',
        )

    monkeypatch.setattr(ht, "run_creation_pipeline", _pipeline)
    api = _FakeAPI()
    api.put_response = _Resp(
        200,
        {"handler_id": "H9", "name": "old-greeter", "description": "greets adventurers"},
    )
    out = await ht.register_handler(_ctx(api), "make the greeter medieval", "message")
    assert "old-greeter" in out and "Updated" in out
    assert api.posted == []
    path, payload = api.updated[0]
    assert path == "/handlers/H9"
    assert payload["script"] == 'await send_message("hail")\n'
    assert payload["description"] == "greets adventurers"


async def test_register_pipeline_error_relayed(monkeypatch):
    async def _err(**kwargs):
        return CreationResult(ok=False, error="cannot exceed 3 messages")

    monkeypatch.setattr(ht, "run_creation_pipeline", _err)
    api = _FakeAPI()
    out = await ht.register_handler(_ctx(api), "spam", "new message")
    assert out == "error: cannot exceed 3 messages"
    assert api.posted == [] and api.updated == []


async def test_register_schedule_below_floor(monkeypatch):
    async def _pipeline(**kwargs):
        return _create_result(
            trigger_type="schedule", settings={"interval_seconds": 5}
        )

    monkeypatch.setattr(ht, "run_creation_pipeline", _pipeline)
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
            {"handler_id": "H1", "name": "greeter", "trigger_type": "message", "description": "a"},
            {"handler_id": "H2", "name": "digest", "trigger_type": "timer", "description": "b"},
        ],
    )
    out = await ht.list_handlers(_ctx(api), "999")
    assert "greeter" in out and "digest" in out and "[timer]" in out


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
