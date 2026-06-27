"""Tests for AdminActor (moderation REST calls)."""

from __future__ import annotations

from smarter_dev.web.admin_actions import AdminActor


class _RecordingActor(AdminActor):
    def __init__(self):
        super().__init__(bot_token="t", guild_id="G1")
        self.calls = []

    async def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))


async def test_ban_user():
    a = _RecordingActor()
    out = await a.ban_user("U1", reason="scam")
    assert "banned U1" in out
    assert a.calls[0][:2] == ("PUT", "/guilds/G1/bans/U1")
    assert "X-Audit-Log-Reason" in a.calls[0][2].get("headers", {})


async def test_kick_user():
    a = _RecordingActor()
    await a.kick_user("U1")
    assert a.calls[0][:2] == ("DELETE", "/guilds/G1/members/U1")


async def test_timeout_user():
    a = _RecordingActor()
    await a.timeout_user("U1", duration_seconds=120)
    method, endpoint, kwargs = a.calls[0]
    assert (method, endpoint) == ("PATCH", "/guilds/G1/members/U1")
    assert "communication_disabled_until" in kwargs["json"]


async def test_delete_message():
    a = _RecordingActor()
    await a.delete_message("C1", "M1")
    assert a.calls[0][:2] == ("DELETE", "/channels/C1/messages/M1")
