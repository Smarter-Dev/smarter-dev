"""Tests for the admin-handlers plugin install step (create vs edit)."""

from __future__ import annotations

from smarter_dev.bot.agents.handler_authoring import AdminCreationResult
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


async def test_install_relays_api_failure():
    api = _FakeAPI()
    api.post_response = _Resp(409, text="name taken")
    line = await install_admin_result(api, "G1", "A1", _result())
    assert "Failed" in line and "name taken" in line
