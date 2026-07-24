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


@pytest.mark.parametrize(
    ("invoke", "expected"),
    [
        (lambda actor: actor.ban_user("U1"), "ban target U1 already absent"),
        (lambda actor: actor.kick_user("U1"), "kick target U1 already absent"),
        (lambda actor: actor.timeout_user("U1"), "timeout target U1 already absent"),
        (
            lambda actor: actor.delete_message("C1", "M1"),
            "message M1 already deleted",
        ),
    ],
)
async def test_absent_moderation_target_is_successful_noop(invoke, expected):
    """A 404 means another actor already achieved the handler's end state."""
    requests: list[httpx.Request] = []
    assert await invoke(_actor(requests, status_code=404)) == expected
    assert len(requests) == 1


@pytest.mark.parametrize(
    "invoke",
    [
        lambda actor: actor.ban_user("U1"),
        lambda actor: actor.kick_user("U1"),
        lambda actor: actor.timeout_user("U1"),
        lambda actor: actor.delete_message("C1", "M1"),
    ],
)
@pytest.mark.parametrize("status_code", [403, 429, 500])
async def test_moderation_action_non_404_error_still_raises(invoke, status_code):
    requests: list[httpx.Request] = []
    with pytest.raises(AdminActionError):
        await invoke(_actor(requests, status_code=status_code))


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


# -- role mutation (add_role / remove_role) + ban purge window (E2) --


def _role_actor(
    requests: list[httpx.Request], status_code: int = 204
) -> AdminActor:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    return AdminActor(
        bot_token="t", guild_id="G1", transport=httpx.MockTransport(handle)
    )


async def test_add_role_puts_role_and_returns_true():
    requests: list[httpx.Request] = []
    result = await _role_actor(requests).add_role("U1", "R1", reason="onboard")
    assert result is True
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/guilds/G1/members/U1/roles/R1")
    assert request.headers["X-Audit-Log-Reason"] == "onboard"


async def test_remove_role_deletes_role_and_returns_true():
    requests: list[httpx.Request] = []
    result = await _role_actor(requests).remove_role("U1", "R1")
    assert result is True
    request = requests[0]
    assert request.method == "DELETE"
    assert request.url.path.endswith("/guilds/G1/members/U1/roles/R1")
    assert "X-Audit-Log-Reason" not in request.headers


async def test_add_role_unknown_member_404_returns_false():
    """A member who left before the grant: silent no-op, no raise."""
    result = await _role_actor([], status_code=404).add_role("U1", "R1")
    assert result is False


async def test_remove_role_unknown_member_404_returns_false():
    result = await _role_actor([], status_code=404).remove_role("U1", "R1")
    assert result is False


async def test_add_role_forbidden_403_raises_admin_action_error():
    for status in (403, 500):
        with pytest.raises(AdminActionError):
            await _role_actor([], status_code=status).add_role("U1", "R1")
        with pytest.raises(AdminActionError):
            await _role_actor([], status_code=status).remove_role("U1", "R1")


async def test_add_role_encodes_audit_reason():
    requests: list[httpx.Request] = []
    await _role_actor(requests).add_role("U1", "R1", reason="sus — telegram")
    assert requests[0].headers["X-Audit-Log-Reason"] == "sus%20%E2%80%94%20telegram"


async def test_ban_user_sends_delete_message_seconds_body():
    requests: list[httpx.Request] = []
    await _role_actor(requests).ban_user(
        "U1", reason="bot heuristic", delete_message_seconds=3600
    )
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/guilds/G1/bans/U1")
    assert json.loads(request.content) == {"delete_message_seconds": 3600}


async def test_ban_user_defaults_delete_message_seconds_zero():
    requests: list[httpx.Request] = []
    await _role_actor(requests).ban_user("U1")
    assert json.loads(requests[0].content) == {"delete_message_seconds": 0}


# -- delete_webhook ---------------------------------------------------------


def _webhook_actor(
    requests: list[httpx.Request], status_code: int = 204
) -> AdminActor:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    return AdminActor(
        bot_token="t", guild_id="G1", transport=httpx.MockTransport(handle)
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://canary.discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://ptb.discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://discordapp.com/api/webhooks/123456789/abcDEF-_token",
    ],
)
async def test_delete_webhook_accepts_valid_discord_url_returns_true_on_204(url):
    requests: list[httpx.Request] = []
    result = await _webhook_actor(requests).delete_webhook(url)
    assert result is True
    request = requests[0]
    assert request.method == "DELETE"
    # Deleted by id/token regardless of which Discord host variant was supplied.
    assert request.url.path.endswith("/webhooks/123456789/abcDEF-_token")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/api/webhooks/1/token",
        "https://discord.com/api/webhooks/123456789",  # missing token
        "https://discord.com/api/webhooks/123/../../users/@me",  # traversal
        "http://discord.com/api/webhooks/123/token",  # not https
        "not-a-url",
        "https://discord.com.evil.com/api/webhooks/1/token",
    ],
)
async def test_delete_webhook_rejects_non_discord_host_raises_admin_action_error(url):
    requests: list[httpx.Request] = []
    with pytest.raises(AdminActionError):
        await _webhook_actor(requests).delete_webhook(url)
    # A rejected URL issues NO request — never an arbitrary-host DELETE.
    assert requests == []


async def test_delete_webhook_returns_false_on_404():
    requests: list[httpx.Request] = []
    result = await _webhook_actor(requests, status_code=404).delete_webhook(
        "https://discord.com/api/webhooks/123456789/tok"
    )
    assert result is False


async def test_delete_webhook_reraises_on_other_error():
    requests: list[httpx.Request] = []
    with pytest.raises(AdminActionError):
        await _webhook_actor(requests, status_code=403).delete_webhook(
            "https://discord.com/api/webhooks/123456789/tok"
        )


# -- get_member_info / search_guild_members ---------------------------------

_ROLES = [
    {"id": "R_ADMIN", "name": "Admin", "position": 10},
    {"id": "R_MOD", "name": "Mod", "position": 5},
    {"id": "R_MEMBER", "name": "Member", "position": 1},
]


def _lookup_actor(
    requests: list[httpx.Request],
    *,
    member_status: int = 200,
    member_payload: dict | None = None,
    user_payload: dict | None = None,
    search_payload: list | None = None,
) -> AdminActor:
    """Actor routing member/user/roles/search reads for the mod-lookup tests."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/roles"):
            return httpx.Response(200, json=_ROLES)
        if "/members/search" in path:
            return httpx.Response(200, json=search_payload or [])
        if "/members/" in path:
            if member_status >= 400:
                return httpx.Response(member_status)
            return httpx.Response(200, json=member_payload)
        if "/users/" in path:
            return httpx.Response(200, json=user_payload)
        return httpx.Response(404)

    return AdminActor(
        bot_token="t", guild_id="G1", transport=httpx.MockTransport(handle)
    )


async def test_get_member_info_returns_guild_member():
    requests: list[httpx.Request] = []
    member_payload = {
        "user": {"id": "170000000000000000", "username": "alice"},
        "nick": "Ali",
        "roles": ["R_MOD", "R_MEMBER"],
        "joined_at": "2021-01-01T00:00:00+00:00",
        "pending": False,
    }
    info = await _lookup_actor(
        requests, member_payload=member_payload
    ).get_member_info("170000000000000000")
    assert info["in_guild"] is True
    assert info["username"] == "alice"
    assert info["nickname"] == "Ali"
    assert info["joined_at"] == "2021-01-01T00:00:00+00:00"
    assert info["is_pending"] is False
    assert info["role_ids"] == ["R_MOD", "R_MEMBER"]
    assert info["role_names"] == ["Mod", "Member"]
    assert info["account_created_at"] is not None


async def test_get_member_info_falls_back_to_user_on_404():
    requests: list[httpx.Request] = []
    info = await _lookup_actor(
        requests,
        member_status=404,
        user_payload={"id": "170000000000000000", "username": "ghost"},
    ).get_member_info("170000000000000000")
    assert info["in_guild"] is False
    assert info["username"] == "ghost"
    assert info["role_ids"] == []
    assert info["role_names"] == []
    assert info["joined_at"] is None
    # Fell through to GET /users/{id} — never fetched roles for a non-member.
    assert any(r.url.path.endswith("/users/170000000000000000") for r in requests)
    assert not any(r.url.path.endswith("/roles") for r in requests)


async def test_search_guild_members_empty_result():
    requests: list[httpx.Request] = []
    result = await _lookup_actor(requests, search_payload=[]).search_guild_members(
        "nobody"
    )
    assert result == {"members": [], "overflow_count": 0}
    # An empty search skips the roles fetch entirely.
    assert not any(r.url.path.endswith("/roles") for r in requests)


def _member_row(uid: str, roles: list[str]) -> dict:
    return {
        "user": {"id": uid, "username": f"u{uid}"},
        "nick": None,
        "roles": roles,
        "joined_at": "2021-01-01T00:00:00+00:00",
    }


async def test_search_guild_members_overflow_exact_below_window_and_floor_when_full():
    requests: list[httpx.Request] = []
    # 15 matched, window not full -> overflow is exact (15 - 10 = 5).
    below = await _lookup_actor(
        requests,
        search_payload=[_member_row(str(i), ["R_MEMBER"]) for i in range(15)],
    ).search_guild_members("u", limit=10)
    assert len(below["members"]) == 10
    assert below["overflow_count"] == 5

    # 100 matched, window full -> overflow is a floor (100 - 10 = 90, rendered "90+").
    full = await _lookup_actor(
        [],
        search_payload=[_member_row(str(i), ["R_MEMBER"]) for i in range(100)],
    ).search_guild_members("u", limit=10)
    assert len(full["members"]) == 10
    assert full["overflow_count"] == 90


async def test_search_guild_members_top_role_highest_position_and_everyone_fallback():
    requests: list[httpx.Request] = []
    result = await _lookup_actor(
        requests,
        search_payload=[
            _member_row("1", ["R_MEMBER", "R_ADMIN"]),  # Admin outranks Member
            _member_row("2", []),  # roleless -> @everyone
        ],
    ).search_guild_members("u", limit=10)
    assert result["members"][0]["top_role_name"] == "Admin"
    assert result["members"][1]["top_role_name"] == "@everyone"


async def test_search_guild_members_query_over_fetches_window():
    requests: list[httpx.Request] = []
    await _lookup_actor(requests, search_payload=[]).search_guild_members(
        "alice", limit=10
    )
    search = next(r for r in requests if "/members/search" in r.url.path)
    # Always over-fetch Discord's 100 window, regardless of the caller's limit.
    assert search.url.params["limit"] == "100"
    assert search.url.params["query"] == "alice"
