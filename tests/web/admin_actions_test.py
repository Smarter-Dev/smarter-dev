"""Tests for AdminActor (an admin fire's REST calls).

These run through the real ``_request`` path over ``httpx.MockTransport`` — a
mock of ``_request`` itself is what let the ban-with-reason double-``headers``
TypeError reach production.
"""

from __future__ import annotations

import json

import httpx
import pytest

from smarter_dev.web.admin_actions import AdminActionError, AdminActor
from tests.moderation_fakes import FakeDiscordRest


@pytest.fixture(autouse=True)
def recorded_events(monkeypatch) -> list:
    """Captures the short-term event log writes instead of reaching Redis.

    The gateway announces each confirmed action once, after it is recorded;
    the mod_action dispatch beside it is stood in for too.
    """
    from smarter_dev.web import guild_event_recorder
    from smarter_dev.web import handler_dispatch

    events: list = []

    async def capture_event(event, **kwargs) -> None:
        events.append(event)

    async def no_dispatch(*args, **kwargs):
        return []

    monkeypatch.setattr(guild_event_recorder, "record_guild_event", capture_event)
    monkeypatch.setattr(handler_dispatch, "dispatch_handler_event", no_dispatch)
    return events


def _discord(status_code: int | None = None) -> FakeDiscordRest:
    discord = FakeDiscordRest(fail_writes=status_code)
    discord.add_member("U1")
    discord.messages["M1"] = {"id": "M1", "author": {"id": "U1"}}
    return discord


def _actor(requests: list[httpx.Request], status_code: int | None = None) -> AdminActor:
    """An actor over a fake guild where U1 is a member who posted M1.

    ``requests`` collects the writes (the gateway's reads of the member, the
    ban and the message are answered but not collected). ``status_code``
    answers every write with that status instead.
    """
    discord = _discord(status_code)
    discord.writes = requests
    return AdminActor(
        bot_token="t",
        guild_id="G1",
        staff_exempt_role_ids=(),
        transport=discord.transport(),
    )


# -- delete_bot_message: upkeep, never a member's message -------------------


async def test_a_bots_message_is_deleted_outside_the_gateway(recorded_events):
    requests: list[httpx.Request] = []

    out = await _bot_message_actor(requests).delete_bot_message("C1", "B1")

    assert out == "deleted message B1"
    request = requests[0]
    assert request.method == "DELETE"
    assert request.url.path.endswith("/channels/C1/messages/B1")
    # Upkeep, not moderation: no incident, no audit row, nothing announced.
    assert recorded_events == []


async def test_a_members_message_is_refused_without_a_delete_call():
    requests: list[httpx.Request] = []

    out = await _actor(requests).delete_bot_message("C1", "M1")

    assert out.startswith("not deleted: message M1 was posted by a member")
    assert requests == []


async def test_an_already_deleted_message_is_a_noop_without_a_delete_call():
    """The author read finds it gone, so no DELETE goes out at all."""
    requests: list[httpx.Request] = []
    out = await _actor(requests).delete_bot_message("C1", "M2")
    assert out == "message M2 already deleted"
    assert requests == []


@pytest.mark.parametrize("status_code", [403, 429, 500])
async def test_a_failed_delete_of_a_bots_message_raises(status_code):
    with pytest.raises(AdminActionError):
        await _bot_message_actor([], status_code=status_code).delete_bot_message(
            "C1", "B1"
        )


def _bot_message_actor(
    requests: list[httpx.Request], status_code: int | None = None
) -> AdminActor:
    """``_actor``'s guild, plus B1: Disboard's notice, a bot's message."""
    discord = _discord(status_code)
    discord.messages["B1"] = {"id": "B1", "author": {"id": "D1", "bot": True}}
    discord.writes = requests
    return AdminActor(
        bot_token="t",
        guild_id="G1",
        staff_exempt_role_ids=(),
        transport=discord.transport(),
    )


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
        bot_token="t",
        guild_id="G1",
        staff_exempt_role_ids=(),
        transport=httpx.MockTransport(handle),
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


async def test_reopen_thread_unarchives():
    requests: list[httpx.Request] = []
    result = await _thread_actor(requests).reopen_thread("T1")
    assert result is True
    mutate = requests[-1]
    assert mutate.method == "PATCH"
    assert json.loads(mutate.content) == {"archived": False}


async def test_thread_ops_return_false_on_gone_target():
    """A janitor sweeping an already-deleted thread is a silent no-op."""
    for op in ("close_thread", "reopen_thread"):
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
            await _thread_actor([], mutate_status=status).reopen_thread("T1")


async def test_thread_ops_reject_non_thread_channel():
    """DELETE /channels/{id} on a text channel would delete it wholesale."""
    for op in ("close_thread", "reopen_thread"):
        requests: list[httpx.Request] = []
        actor = _thread_actor(requests, channel_payload={"type": 0, "guild_id": "G1"})
        with pytest.raises(AdminActionError, match="not a thread"):
            await getattr(actor, op)("C1")
        assert [r.method for r in requests] == ["GET"], "must not touch a non-thread"


async def test_thread_ops_reject_thread_outside_guild():
    requests: list[httpx.Request] = []
    actor = _thread_actor(requests, channel_payload={"type": 11, "guild_id": "G2"})
    with pytest.raises(AdminActionError, match="outside guild"):
        await actor.close_thread("T1")
    assert [r.method for r in requests] == ["GET"]


async def test_thread_verification_cached_across_ops():
    """A close-then-reopen sweep fetches the channel once, not per mutation."""
    requests: list[httpx.Request] = []
    actor = _thread_actor(requests)
    await actor.close_thread("T1")
    await actor.reopen_thread("T1")
    assert [r.method for r in requests] == ["GET", "PATCH", "PATCH"]


# -- role mutation (add_role / remove_role) --


MANAGE_MESSAGES = 1 << 13
MENTION_EVERYONE = 1 << 17
MANAGE_THREADS = 1 << 34
STAFF_ROLE = "644390354157568014"

#: The guild the role rule reads: R1 an ordinary role, R_MOD one carrying a
#: staff permission, R_CHAN one granted a staff permission in a channel.
GUILD_ROLES = [
    {"id": "G1", "permissions": "0"},
    {"id": "R1", "permissions": "0"},
    {"id": STAFF_ROLE, "permissions": "0"},
    {"id": "R_MOD", "permissions": str(MANAGE_MESSAGES)},
    {"id": "R_CHAN", "permissions": "0"},
    {"id": "R_PING", "permissions": str(MENTION_EVERYONE)},
    {"id": "R_THREADS", "permissions": "0"},
]
GUILD_CHANNELS = [
    {
        "id": "C1",
        "permission_overwrites": [
            {"id": "R_CHAN", "type": 0, "allow": str(MANAGE_MESSAGES), "deny": "0"},
            {"id": "R_THREADS", "type": 0, "allow": str(MANAGE_THREADS), "deny": "0"},
        ],
    }
]


def _role_actor(
    requests: list[httpx.Request],
    status_code: int = 204,
    *,
    guild_status: int = 200,
) -> AdminActor:
    """Writes answer ``status_code``; the guild and channel reads the role rule
    needs answer ``guild_status`` (and are not collected)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if guild_status != 200:
                return httpx.Response(guild_status)
            if request.url.path.endswith("/guilds/G1"):
                return httpx.Response(
                    200, json={"id": "G1", "owner_id": "O1", "roles": GUILD_ROLES}
                )
            if request.url.path.endswith("/guilds/G1/channels"):
                return httpx.Response(200, json=GUILD_CHANNELS)
            if "/members/" in request.url.path:
                return httpx.Response(200, json={"user": {"id": "U1"}, "roles": []})
            if request.url.path.endswith("/users/@me"):
                return httpx.Response(200, json={"id": "BOT"})
            return httpx.Response(404)
        requests.append(request)
        return httpx.Response(status_code)

    return AdminActor(
        bot_token="t",
        guild_id="G1",
        staff_exempt_role_ids=(),
        transport=httpx.MockTransport(handle),
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


@pytest.mark.parametrize(
    ("role_id", "why"),
    [
        (STAFF_ROLE, "is a staff role"),
        ("R_MOD", "carries a staff permission"),
        ("R_CHAN", "carries a staff permission in a channel"),
        ("R_GONE", "could not be checked"),
        ("G1", "@everyone"),
        # #255: not staff standing, but mass pings and thread moderation.
        ("R_PING", "carries an elevated permission"),
        ("R_THREADS", "carries an elevated permission"),
    ],
)
async def test_a_role_that_confers_staff_standing_is_never_changed(role_id, why):
    """#239: strip a moderator's role in one fire, ban them in the next. The
    role change itself is refused, both ways, before any write."""
    from smarter_dev.web.admin_actions import ProtectedRoleError

    requests: list[httpx.Request] = []
    actor = _role_actor(requests)
    with pytest.raises(ProtectedRoleError, match=why):
        await actor.remove_role("U1", role_id)
    with pytest.raises(ProtectedRoleError, match=why):
        await actor.add_role("U1", role_id)
    assert requests == []


async def test_a_role_whose_standing_cannot_be_read_is_refused():
    from smarter_dev.web.admin_actions import ProtectedRoleError

    requests: list[httpx.Request] = []
    with pytest.raises(ProtectedRoleError, match="could not be checked"):
        await _role_actor(requests, guild_status=503).add_role("U1", "R1")
    assert requests == []


async def test_add_role_encodes_audit_reason():
    requests: list[httpx.Request] = []
    await _role_actor(requests).add_role("U1", "R1", reason="sus — telegram")
    assert requests[0].headers["X-Audit-Log-Reason"] == "sus%20%E2%80%94%20telegram"


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
        bot_token="t",
        guild_id="G1",
        staff_exempt_role_ids=(),
        transport=httpx.MockTransport(handle),
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
