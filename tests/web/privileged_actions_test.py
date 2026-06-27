"""Tests for the privileged moderation action executor."""

from __future__ import annotations

import pytest

from smarter_dev.web.privileged_actions import (
    PrivilegedActionError,
    PrivilegedActor,
    validate_action,
)


class _RecordingActor(PrivilegedActor):
    def __init__(self):
        super().__init__(bot_token="t")
        self.calls = []

    async def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))


async def test_timeout_calls_patch_member():
    actor = _RecordingActor()
    out = await actor.execute(
        {"kind": "timeout", "target_user_id": "U1", "duration_seconds": 120}, "G1"
    )
    assert "timed out U1" in out
    method, endpoint, kwargs = actor.calls[0]
    assert method == "PATCH" and endpoint == "/guilds/G1/members/U1"
    assert "communication_disabled_until" in kwargs["json"]


async def test_kick_calls_delete_member():
    actor = _RecordingActor()
    await actor.execute({"kind": "kick", "target_user_id": "U1"}, "G1")
    assert actor.calls[0][:2] == ("DELETE", "/guilds/G1/members/U1")


async def test_ban_calls_put_ban():
    actor = _RecordingActor()
    await actor.execute({"kind": "ban", "target_user_id": "U1"}, "G1")
    assert actor.calls[0][:2] == ("PUT", "/guilds/G1/bans/U1")


async def test_delete_message():
    actor = _RecordingActor()
    await actor.execute(
        {"kind": "delete", "channel_id": "C1", "message_id": "M1"}, "G1"
    )
    assert actor.calls[0][:2] == ("DELETE", "/channels/C1/messages/M1")


async def test_unknown_kind_raises():
    actor = _RecordingActor()
    with pytest.raises(PrivilegedActionError):
        await actor.execute({"kind": "nuke"}, "G1")


async def test_missing_target_raises():
    actor = _RecordingActor()
    with pytest.raises(PrivilegedActionError):
        await actor.execute({"kind": "timeout"}, "G1")


def test_validate_action():
    validate_action({"kind": "ban", "target_user_id": "U1"})
    with pytest.raises(PrivilegedActionError):
        validate_action({"kind": "delete", "channel_id": "C1"})  # missing message_id
