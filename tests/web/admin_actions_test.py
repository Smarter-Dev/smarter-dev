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


def _thread_actor(
    requests: list[httpx.Request],
    channel_payload: dict | None = None,
    get_status: int = 200,
    mutate_status: int = 200,
) -> AdminActor:
    """Actor whose transport answers GET /channels/{id} with a channel object.

    Every mutating thread op first verifies its target through that GET; the
    default payload is a public thread in the actor's own guild.
    """

    payload = channel_payload or {"type": 11, "guild_id": "G1"}

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            if get_status >= 400:
                return httpx.Response(get_status)
            return httpx.Response(get_status, json=payload)
        return httpx.Response(mutate_status)

    return AdminActor(
        bot_token="t", guild_id="G1", transport=httpx.MockTransport(handle)
    )


async def test_close_thread_verifies_then_archives():
    requests: list[httpx.Request] = []
    result = await _thread_actor(requests).close_thread("T1")
    assert result is True
    verify, mutate = requests
    assert verify.method == "GET"
    assert verify.url.path.endswith("/channels/T1")
    assert mutate.method == "PATCH"
    assert mutate.url.path.endswith("/channels/T1")
    assert json.loads(mutate.content) == {"archived": True}


async def test_lock_thread_locks_and_archives():
    requests: list[httpx.Request] = []
    result = await _thread_actor(requests).lock_thread("T1")
    assert result is True
    assert json.loads(requests[-1].content) == {"locked": True, "archived": True}


async def test_reopen_thread_unarchives():
    requests: list[httpx.Request] = []
    result = await _thread_actor(requests).reopen_thread("T1")
    assert result is True
    mutate = requests[-1]
    assert mutate.method == "PATCH"
    assert json.loads(mutate.content) == {"archived": False}


async def test_delete_thread_deletes():
    requests: list[httpx.Request] = []
    result = await _thread_actor(requests).delete_thread("T1")
    assert result is True
    mutate = requests[-1]
    assert mutate.method == "DELETE"
    assert mutate.url.path.endswith("/channels/T1")


async def test_thread_ops_return_false_on_gone_target():
    """A janitor sweeping an already-deleted thread is a silent no-op."""
    for op in ("close_thread", "lock_thread", "reopen_thread", "delete_thread"):
        requests: list[httpx.Request] = []
        actor = _thread_actor(requests, get_status=404)
        assert await getattr(actor, op)("T1") is False
        assert [r.method for r in requests] == ["GET"], "must not mutate a gone target"


async def test_thread_ops_return_false_when_mutation_hits_404():
    """Thread deleted between verification and mutation: still a no-op."""
    requests: list[httpx.Request] = []
    assert await _thread_actor(requests, mutate_status=404).close_thread("T1") is False


async def test_thread_ops_raise_on_non_404_error():
    for status in (403, 500):
        with pytest.raises(AdminActionError):
            await _thread_actor([], mutate_status=status).close_thread("T1")
        with pytest.raises(AdminActionError):
            await _thread_actor([], mutate_status=status).delete_thread("T1")


async def test_thread_ops_reject_non_thread_channel():
    """DELETE /channels/{id} on a text channel would delete it wholesale."""
    for op in ("close_thread", "lock_thread", "reopen_thread", "delete_thread"):
        requests: list[httpx.Request] = []
        actor = _thread_actor(requests, channel_payload={"type": 0, "guild_id": "G1"})
        with pytest.raises(AdminActionError, match="not a thread"):
            await getattr(actor, op)("C1")
        assert [r.method for r in requests] == ["GET"], "must not touch a non-thread"


async def test_thread_ops_reject_thread_outside_guild():
    requests: list[httpx.Request] = []
    actor = _thread_actor(requests, channel_payload={"type": 11, "guild_id": "G2"})
    with pytest.raises(AdminActionError, match="outside guild"):
        await actor.delete_thread("T1")
    assert [r.method for r in requests] == ["GET"]


async def test_thread_verification_cached_across_ops():
    """A close-then-lock sweep fetches the channel once, not per mutation."""
    requests: list[httpx.Request] = []
    actor = _thread_actor(requests)
    await actor.close_thread("T1")
    await actor.lock_thread("T1")
    assert [r.method for r in requests] == ["GET", "PATCH", "PATCH"]
