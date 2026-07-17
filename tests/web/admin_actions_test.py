"""Tests for AdminActor (moderation REST calls).

These run through the real ``_request`` path over ``httpx.MockTransport`` — a
mock of ``_request`` itself is what let the ban-with-reason double-``headers``
TypeError reach production.
"""

from __future__ import annotations

import json

import httpx
import pytest

from smarter_dev.web.admin_actions import AdminActionError, AdminActor


def _actor(requests: list[httpx.Request], status_code: int = 204) -> AdminActor:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    return AdminActor(
        bot_token="t", guild_id="G1", transport=httpx.MockTransport(handle)
    )


async def test_ban_user_with_reason_sends_auth_and_audit_headers():
    """Regression: a ban with a reason must not crash on duplicate headers."""
    requests: list[httpx.Request] = []
    out = await _actor(requests).ban_user("U1", reason="scam")
    assert "banned U1" in out
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/guilds/G1/bans/U1")
    assert request.headers["Authorization"] == "Bot t"
    assert request.headers["X-Audit-Log-Reason"] == "scam"


async def test_ban_user_without_reason():
    requests: list[httpx.Request] = []
    await _actor(requests).ban_user("U1")
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/guilds/G1/bans/U1")
    assert "X-Audit-Log-Reason" not in request.headers


async def test_ban_reason_is_url_encoded_for_the_audit_header():
    """Non-latin-1 reasons must not crash httpx's header encoding."""
    requests: list[httpx.Request] = []
    await _actor(requests).ban_user("U1", reason="crypto scam — telegram")
    assert requests[0].headers["X-Audit-Log-Reason"] == "crypto%20scam%20%E2%80%94%20telegram"


async def test_kick_user():
    requests: list[httpx.Request] = []
    await _actor(requests).kick_user("U1")
    request = requests[0]
    assert request.method == "DELETE"
    assert request.url.path.endswith("/guilds/G1/members/U1")


async def test_timeout_user():
    requests: list[httpx.Request] = []
    await _actor(requests).timeout_user("U1", duration_seconds=120)
    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith("/guilds/G1/members/U1")
    assert "communication_disabled_until" in json.loads(request.content)


async def test_delete_message():
    requests: list[httpx.Request] = []
    await _actor(requests).delete_message("C1", "M1")
    request = requests[0]
    assert request.method == "DELETE"
    assert request.url.path.endswith("/channels/C1/messages/M1")


async def test_error_status_raises_admin_action_error():
    requests: list[httpx.Request] = []
    with pytest.raises(AdminActionError):
        await _actor(requests, status_code=403).kick_user("U1")


# -- thread operations ------------------------------------------------------


async def test_close_thread_archives():
    requests: list[httpx.Request] = []
    result = await _actor(requests, status_code=200).close_thread("T1")
    assert result is True
    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith("/channels/T1")
    assert json.loads(request.content) == {"archived": True}


async def test_lock_thread_locks_and_archives():
    requests: list[httpx.Request] = []
    result = await _actor(requests, status_code=200).lock_thread("T1")
    assert result is True
    assert json.loads(requests[0].content) == {"locked": True, "archived": True}


async def test_reopen_thread_unarchives():
    requests: list[httpx.Request] = []
    result = await _actor(requests, status_code=200).reopen_thread("T1")
    assert result is True
    request = requests[0]
    assert request.method == "PATCH"
    assert json.loads(request.content) == {"archived": False}


async def test_delete_thread_deletes():
    requests: list[httpx.Request] = []
    result = await _actor(requests, status_code=200).delete_thread("T1")
    assert result is True
    request = requests[0]
    assert request.method == "DELETE"
    assert request.url.path.endswith("/channels/T1")


async def test_thread_ops_return_false_on_404():
    """A janitor sweeping an already-deleted thread is a silent no-op."""
    for status in (404,):
        assert await _actor([], status_code=status).close_thread("T1") is False
        assert await _actor([], status_code=status).lock_thread("T1") is False
        assert await _actor([], status_code=status).reopen_thread("T1") is False
        assert await _actor([], status_code=status).delete_thread("T1") is False


async def test_thread_ops_raise_on_non_404_error():
    for status in (403, 500):
        with pytest.raises(AdminActionError):
            await _actor([], status_code=status).close_thread("T1")
        with pytest.raises(AdminActionError):
            await _actor([], status_code=status).delete_thread("T1")
