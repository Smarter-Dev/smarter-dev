"""Tests for the admin-handlers plugin install step (create vs edit)."""

from __future__ import annotations

import pytest

from smarter_dev.bot.agents import handler_authoring
from smarter_dev.bot.agents.handler_authoring import AdminCreationResult
from smarter_dev.bot.plugins import admin_handlers
from smarter_dev.bot.plugins.admin_handlers import _run_create_admin_handler
from smarter_dev.bot.plugins.admin_handlers import install_admin_result


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeAPI:
    def __init__(self):
        self.posted = []
        self.updated = []
        self.post_response = _Resp(
            201,
            {
                "handler_id": "AH1",
                "name": "raid-alarm",
                "trigger_type": "message",
                "channel_ids": [],
                "description": "alerts on raids",
            },
        )
        self.put_response = _Resp(
            200,
            {
                "handler_id": "AH2",
                "name": "scam-banner",
                "trigger_type": "message",
                "channel_ids": ["MODCHAT"],
                "description": "bans scammers, checks attachments",
            },
        )

    async def post(self, path, json_data=None):
        self.posted.append((path, json_data))
        return self.post_response

    async def put(self, path, json_data=None):
        self.updated.append((path, json_data))
        return self.put_response


def _result(**over):
    fields = {
        "ok": True,
        "action": "create",
        "target_handler_id": None,
        "name": "raid-alarm",
        "trigger_type": "message",
        "channel_ids": [],
        "settings": {},
        "description": "alerts on raids",
        "script": 'await send_message("raid!")\n',
    }
    fields.update(over)
    return AdminCreationResult(**fields)


async def test_install_creates_named_admin_handler():
    api = _FakeAPI()
    line = await install_admin_result(api, "G1", "A1", _result())
    assert "raid-alarm" in line and "Created" in line
    path, payload = api.posted[0]
    assert path == "/admin/handlers"
    assert payload["name"] == "raid-alarm"
    assert payload["created_by_admin"] == "A1"


async def test_install_edits_existing_admin_handler():
    api = _FakeAPI()
    line = await install_admin_result(
        api,
        "G1",
        "A1",
        _result(
            action="edit",
            target_handler_id="AH2",
            name="scam-banner",
            channel_ids=["MODCHAT"],
            description="bans scammers, checks attachments",
        ),
    )
    assert "scam-banner" in line and "Updated" in line
    assert api.posted == []
    path, payload = api.updated[0]
    assert path == "/admin/handlers/AH2"
    assert payload["channel_ids"] == ["MODCHAT"]


async def test_install_creates_member_event_handler():
    # The install step is trigger-agnostic: a member_join admin handler installs
    # exactly like a message one, carrying the new trigger type to the API.
    api = _FakeAPI()
    api.post_response = _Resp(
        201,
        {
            "handler_id": "AH3",
            "name": "join-alert",
            "trigger_type": "member_join",
            "channel_ids": [],
            "description": "alerts mods on join",
        },
    )
    line = await install_admin_result(
        api,
        "G1",
        "A1",
        _result(
            trigger_type="member_join",
            name="join-alert",
            description="alerts mods on join",
        ),
    )
    assert "join-alert" in line and "Created" in line
    _, payload = api.posted[0]
    assert payload["trigger_type"] == "member_join"


async def test_install_relays_api_failure():
    api = _FakeAPI()
    api.post_response = _Resp(409, text="name taken")
    line = await install_admin_result(api, "G1", "A1", _result())
    assert "Failed" in line and "name taken" in line


# -- the /adminhandler create flow ---------------------------------------------


class _FakeAuthor:
    id = "A1"


class _FakeCtx:
    guild_id = "G1"
    author = _FakeAuthor()

    def __init__(self):
        self.deferred = 0
        self.edits = []

    async def respond(self, *args, **kwargs):
        self.deferred += 1

    async def edit_last_response(self, text):
        self.edits.append(text)


def _wire_create_flow(monkeypatch, pipeline):
    async def allow_admin(ctx, message):
        return False

    async def no_handlers(api, guild_id):
        return []

    monkeypatch.setattr(admin_handlers, "deny_if_not_admin", allow_admin)
    monkeypatch.setattr(admin_handlers, "_api_client", lambda: _FakeAPI())
    monkeypatch.setattr(
        admin_handlers, "_guild_admin_handlers_with_scripts", no_handlers
    )
    monkeypatch.setattr(handler_authoring, "run_admin_creation_pipeline", pipeline)


async def test_create_relays_pipeline_progress_to_the_admin(monkeypatch):
    async def pipeline(*, progress, **kwargs):
        await progress("First draft needs work — the reviewer rejected it.")
        return _result()

    _wire_create_flow(monkeypatch, pipeline)
    ctx = _FakeCtx()

    await _run_create_admin_handler(ctx, "alert mods about raids")

    assert any("First draft needs work" in text for text in ctx.edits)
    assert "Created" in ctx.edits[-1]


async def test_create_reports_a_rejected_result(monkeypatch):
    async def pipeline(**kwargs):
        return AdminCreationResult(ok=False, error="the reviewer rejected it: nope")

    _wire_create_flow(monkeypatch, pipeline)
    ctx = _FakeCtx()

    await _run_create_admin_handler(ctx, "alert mods about raids")

    assert ctx.edits[-1] == "Couldn't do it — the reviewer rejected it: nope"


async def test_create_reports_a_pipeline_crash_instead_of_hanging(monkeypatch):
    async def pipeline(**kwargs):
        raise RuntimeError("provider exploded")

    _wire_create_flow(monkeypatch, pipeline)
    ctx = _FakeCtx()

    with pytest.raises(RuntimeError):
        await _run_create_admin_handler(ctx, "alert mods about raids")

    assert ctx.deferred == 1
    assert any("Something broke" in text for text in ctx.edits)
