"""Tests for DiscordEmitter (worker-tier message/reaction REST calls)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from smarter_dev.web.handler_emitter import (
    DiscordEmitError,
    DiscordEmitter,
    _snowflake_created_at,
)


def _emitter(
    requests: list[httpx.Request], status_code: int = 200, body: str = "{}"
) -> DiscordEmitter:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text=body)

    return DiscordEmitter(bot_token="t", transport=httpx.MockTransport(handle))


def _routed_emitter(
    handle: Callable[[httpx.Request], httpx.Response], guild_id: str = "G1"
) -> DiscordEmitter:
    """An emitter whose responses vary by request (multi-call methods)."""
    return DiscordEmitter(
        bot_token="t", guild_id=guild_id, transport=httpx.MockTransport(handle)
    )


async def test_create_message_posts_content_and_returns_id():
    requests: list[httpx.Request] = []
    message_id = await _emitter(requests, body='{"id": "M9"}').create_message(
        "C1", "hello"
    )
    assert message_id == "M9"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/channels/C1/messages")
    assert request.headers["Authorization"] == "Bot t"
    payload = json.loads(request.content)
    assert payload["content"] == "hello"
    # Link-preview embeds are suppressed so URL-heavy output doesn't flood.
    assert payload["flags"] == 1 << 2
    # By default no handler send may ping @everyone/@here or a role.
    assert payload["allowed_mentions"] == {"parse": ["users"]}


async def test_create_message_suppresses_role_and_everyone_mentions_by_default():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "M9"}').create_message("C1", "@everyone")
    payload = json.loads(requests[0].content)
    # Roles / @everyone / @here are stripped; only user mentions parse.
    assert payload["allowed_mentions"] == {"parse": ["users"]}
    assert payload["flags"] == 1 << 2


async def test_create_message_with_ping_role_id_allows_that_role():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "M9"}').create_message(
        "C1", "mods!", ping_role_id="R1"
    )
    payload = json.loads(requests[0].content)
    assert payload["allowed_mentions"] == {"parse": ["users"], "roles": ["R1"]}


async def test_create_message_truncates_to_discord_limit():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "M9"}').create_message("C1", "x" * 3000)
    assert len(json.loads(requests[0].content)["content"]) == 2000


async def test_add_reaction_encodes_emoji():
    requests: list[httpx.Request] = []
    await _emitter(requests, status_code=204).add_reaction(
        "C1", "M1", "<party:12345>"
    )
    request = requests[0]
    assert request.method == "PUT"
    # str(url) preserves the wire encoding; url.path would show it decoded.
    assert str(request.url).endswith(
        "/channels/C1/messages/M1/reactions/party%3A12345/@me"
    )


async def test_error_status_raises_emit_error():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="nope").create_message(
            "C1", "hello"
        )


async def test_create_message_tolerate_missing_target_returns_false_on_403_404():
    # An explicit cross-channel/thread target (relay forum post) that is gone or
    # inaccessible is an EXPECTED outcome the relay script branches to ❌ on, so
    # with tolerate_missing_target it returns False instead of raising.
    for status in (403, 404):
        requests: list[httpx.Request] = []
        result = await _emitter(requests, status_code=status, body="gone").create_message(
            "POST1", "relayed reply", tolerate_missing_target=True
        )
        assert result is False


async def test_create_message_tolerate_missing_target_still_raises_on_5xx():
    # A 5xx is an infrastructure failure, never a branch — it raises even when
    # tolerating a missing target.
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=500, body="boom").create_message(
            "POST1", "reply", tolerate_missing_target=True
        )


async def test_create_message_without_tolerance_raises_on_404():
    # The default (home-channel) send keeps the raise-on-4xx contract.
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=404, body="gone").create_message(
            "C1", "hello"
        )


# -- edit_message -----------------------------------------------------------


async def test_edit_message_patches_content_and_returns_id():
    requests: list[httpx.Request] = []
    message_id = await _emitter(requests, body='{"id": "M9"}').edit_message(
        "C1", "M9", "updated rules"
    )
    assert message_id == "M9"
    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith("/channels/C1/messages/M9")
    assert json.loads(request.content)["content"] == "updated rules"


async def test_edit_message_truncates_to_discord_limit():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "M9"}').edit_message("C1", "M9", "x" * 3000)
    assert len(json.loads(requests[0].content)["content"]) == 2000


async def test_edit_message_payload_suppresses_embeds_and_mentions():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "M9"}').edit_message("C1", "M9", "@everyone")
    payload = json.loads(requests[0].content)
    # Link-preview embeds suppressed; mass/role pings stripped (only users parse).
    assert payload["flags"] == 1 << 2
    assert payload["allowed_mentions"] == {"parse": ["users"]}


async def test_edit_message_raises_on_403_not_bot_authored():
    # Editing a message the bot didn't author is a REST 403 — no silent fallback.
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="not yours").edit_message(
            "C1", "M9", "nope"
        )


async def test_edit_message_raises_on_404_message_deleted():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=404, body="gone").edit_message(
            "C1", "M9", "nope"
        )


# -- rename_channel ---------------------------------------------------------


async def test_rename_channel_patches_name_and_returns():
    requests: list[httpx.Request] = []
    result = await _emitter(requests, body='{"id": "C1"}').rename_channel(
        "C1", "📊Members: 1.2k"
    )
    assert result is True
    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith("/channels/C1")
    assert json.loads(request.content) == {"name": "📊Members: 1.2k"}


async def test_rename_channel_truncates_name_to_hundred():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "C1"}').rename_channel("C1", "y" * 150)
    assert len(json.loads(requests[0].content)["name"]) == 100


async def test_rename_channel_raises_on_error():
    # Missing MANAGE_CHANNELS (403) or any non-2xx errors the fire loudly.
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="no perms").rename_channel(
            "C1", "x"
        )


# -- list_threads -----------------------------------------------------------

_ACTIVE_THREADS_BODY = json.dumps(
    {
        "threads": [
            {
                "id": "T1",
                "name": "active-here",
                "parent_id": "C1",
                "owner_id": "U1",
                "message_count": 4,
                "thread_metadata": {
                    "archived": False,
                    "locked": False,
                    "create_timestamp": "2026-07-01T00:00:00+00:00",
                },
                "applied_tags": ["TAG1"],
            },
            {
                # different parent channel — must be filtered out
                "id": "T2",
                "name": "elsewhere",
                "parent_id": "C9",
                "owner_id": "U2",
                "message_count": 1,
                "thread_metadata": {"archived": False, "locked": False},
            },
        ]
    }
)
_ARCHIVED_THREADS_BODY = json.dumps(
    {
        "threads": [
            {
                "id": "T3",
                "name": "archived-here",
                "parent_id": "C1",
                "owner_id": "U3",
                "message_count": 9,
                "thread_metadata": {
                    "archived": True,
                    "locked": True,
                    "create_timestamp": "2026-06-01T00:00:00+00:00",
                },
            }
        ],
        "has_more": False,
    }
)
_PARENT_CHANNEL_BODY = json.dumps(
    {
        "id": "C1",
        "type": 15,
        "available_tags": [
            {"id": "TAG1", "name": "bug"},
            {"id": "TAG2", "name": "feature"},
        ],
    }
)


def _thread_list_handler(
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/guilds/G1/threads/active"):
            return httpx.Response(200, text=_ACTIVE_THREADS_BODY)
        if path.endswith("/channels/C1/threads/archived/public"):
            return httpx.Response(200, text=_ARCHIVED_THREADS_BODY)
        if path.endswith("/channels/C1"):
            return httpx.Response(200, text=_PARENT_CHANNEL_BODY)
        return httpx.Response(404, text="{}")

    return handle


async def test_list_threads_merges_active_and_archived_filtered_to_parent():
    requests: list[httpx.Request] = []
    threads = await _routed_emitter(_thread_list_handler(requests)).list_threads("C1")

    # T2 (parent C9) filtered out; active first, then archived.
    assert [t["thread_id"] for t in threads] == ["T1", "T3"]
    active = threads[0]
    assert active == {
        "thread_id": "T1",
        "name": "active-here",
        "created_at": "2026-07-01T00:00:00+00:00",
        "archived": False,
        "locked": False,
        "owner_id": "U1",
        "message_count": 4,
        "applied_tag_names": ["bug"],
    }
    archived = threads[1]
    assert archived["archived"] is True
    assert archived["locked"] is True
    assert archived["applied_tag_names"] == []


async def test_list_threads_passes_limit_to_archived_request():
    requests: list[httpx.Request] = []
    await _routed_emitter(_thread_list_handler(requests)).list_threads("C1", limit=7)
    archived_request = next(
        r for r in requests if r.url.path.endswith("/threads/archived/public")
    )
    assert archived_request.url.params["limit"] == "7"


async def test_list_threads_skips_tag_fetch_when_no_thread_has_tags():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/guilds/G1/threads/active"):
            return httpx.Response(200, text=json.dumps({"threads": []}))
        if path.endswith("/channels/C1/threads/archived/public"):
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "threads": [
                            {
                                "id": "T3",
                                "name": "plain",
                                "parent_id": "C1",
                                "owner_id": "U3",
                                "message_count": 0,
                                "thread_metadata": {
                                    "archived": True,
                                    "locked": False,
                                },
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(500, text="parent channel should not be fetched")

    threads = await _routed_emitter(handle).list_threads("C1")
    assert threads[0]["applied_tag_names"] == []
    # No bare GET /channels/C1 was issued (only the archived sub-path).
    assert not any(
        r.url.path.endswith("/channels/C1") for r in requests
    )


async def test_list_threads_caps_at_fifty():
    requests: list[httpx.Request] = []
    many = [
        {
            "id": f"A{i}",
            "name": "x",
            "parent_id": "C1",
            "owner_id": "U",
            "message_count": 0,
            "thread_metadata": {"archived": False, "locked": False},
        }
        for i in range(60)
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/guilds/G1/threads/active"):
            return httpx.Response(200, text=json.dumps({"threads": many}))
        return httpx.Response(200, text=json.dumps({"threads": []}))

    threads = await _routed_emitter(handle).list_threads("C1")
    assert len(threads) == 50


async def test_list_threads_returns_empty_on_404():
    requests: list[httpx.Request] = []
    threads = await _routed_emitter(
        lambda r: (requests.append(r), httpx.Response(404, text="gone"))[1]
    ).list_threads("C1")
    assert threads == []


async def test_list_threads_raises_on_non_404_error():
    with pytest.raises(DiscordEmitError):
        await _routed_emitter(
            lambda r: httpx.Response(403, text="forbidden")
        ).list_threads("C1")


# -- create_thread ----------------------------------------------------------


async def test_create_thread_from_message():
    requests: list[httpx.Request] = []
    thread_id = await _emitter(requests, body='{"id": "T7"}').create_thread(
        "C1", "discussion", message_id="M5"
    )
    assert thread_id == "T7"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/channels/C1/messages/M5/threads")
    assert json.loads(request.content) == {"name": "discussion"}


async def test_create_thread_standalone_is_public_type_11():
    requests: list[httpx.Request] = []
    thread_id = await _emitter(requests, body='{"id": "T8"}').create_thread(
        "C1", "standalone"
    )
    assert thread_id == "T8"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/channels/C1/threads")
    payload = json.loads(request.content)
    assert payload == {"name": "standalone", "type": 11}


async def test_create_thread_raises_on_any_failure_including_404():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=404, body="gone").create_thread(
            "C1", "x"
        )


# -- create_post ------------------------------------------------------------


async def test_create_post_resolves_tag_names_to_ids():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=_PARENT_CHANNEL_BODY)
        return httpx.Response(201, text='{"id": "P1"}')

    post_id = await _routed_emitter(handle).create_post(
        "C1", "How do I?", "help me", tag_names=["feature"]
    )
    assert post_id == "P1"
    post_request = next(r for r in requests if r.method == "POST")
    assert post_request.url.path.endswith("/channels/C1/threads")
    payload = json.loads(post_request.content)
    assert payload["name"] == "How do I?"
    assert payload["message"] == {
        "content": "help me",
        "allowed_mentions": {"parse": ["users"]},
    }
    assert payload["applied_tags"] == ["TAG2"]


async def test_create_post_without_tags_sends_no_applied_tags():
    requests: list[httpx.Request] = []
    post_id = await _emitter(requests, body='{"id": "P2"}').create_post(
        "C1", "title", "body"
    )
    assert post_id == "P2"
    # Only the create POST — no channel fetch to resolve tags.
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert "applied_tags" not in payload
    # A forum post's body can ping, so its starter message is suppressed too.
    assert payload["message"]["allowed_mentions"] == {"parse": ["users"]}


async def test_create_post_targets_forum_channel_with_initial_message():
    # A DM-relay member post is created against a GUILD_FORUM channel via the
    # forum-thread endpoint (POST /channels/{forum}/threads with an embedded
    # starter `message`), returning the created post/thread id — the exact
    # Discord contract for forum posts (not the text-channel /messages/{id}/threads
    # shape). This is what create_post backs, so relay-support reuses it as-is.
    requests: list[httpx.Request] = []
    post_id = await _emitter(requests, body='{"id": "POST42"}').create_post(
        "FORUM1", "DM from Alice", "hey staff, I need help"
    )
    assert post_id == "POST42"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/channels/FORUM1/threads")
    payload = json.loads(request.content)
    assert payload["name"] == "DM from Alice"
    assert payload["message"]["content"] == "hey staff, I need help"


async def test_create_post_unknown_tag_raises_value_error_listing_valid():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_PARENT_CHANNEL_BODY)

    with pytest.raises(ValueError) as exc_info:
        await _routed_emitter(handle).create_post(
            "C1", "title", "body", tag_names=["nonsense"]
        )
    message = str(exc_info.value)
    assert "nonsense" in message
    assert "bug" in message and "feature" in message


# -- get_thread_parent_id ---------------------------------------------------


async def test_get_thread_parent_id_returns_parent_for_thread_type():
    requests: list[httpx.Request] = []
    parent = await _emitter(
        requests, body='{"id": "T1", "type": 11, "parent_id": "C1"}'
    ).get_thread_parent_id("T1")
    assert parent == "C1"
    assert requests[0].method == "GET"
    assert requests[0].url.path.endswith("/channels/T1")


async def test_get_thread_parent_id_returns_none_for_non_thread_channel():
    requests: list[httpx.Request] = []
    parent = await _emitter(
        requests, body='{"id": "C1", "type": 0, "parent_id": "CAT1"}'
    ).get_thread_parent_id("C1")
    assert parent is None


async def test_get_thread_parent_id_returns_none_on_404():
    requests: list[httpx.Request] = []
    parent = await _emitter(
        requests, status_code=404, body="gone"
    ).get_thread_parent_id("T1")
    assert parent is None


async def test_get_thread_parent_id_raises_on_non_404_error():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="nope").get_thread_parent_id(
            "T1"
        )


# -- get_guild_member_count -------------------------------------------------


async def test_get_guild_member_count_returns_approximate_count():
    requests: list[httpx.Request] = []
    emitter = DiscordEmitter(
        bot_token="t",
        guild_id="G1",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request),
                httpx.Response(200, text='{"approximate_member_count": 1234}'),
            )[1]
        ),
    )
    count = await emitter.get_guild_member_count()
    assert count == 1234
    assert isinstance(count, int)
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path.endswith("/guilds/G1")
    # with_counts is what makes approximate_member_count present in the payload.
    assert request.url.params.get("with_counts") == "true"


async def test_get_guild_member_count_raises_on_rest_error():
    # A non-200 propagates (no silent 0) so a stat handler errors loudly.
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="no perms").get_guild_member_count()


# ---------------------------------------------------------------------------
# send_dm — open (or reuse) the DM channel, then post (E2)
# ---------------------------------------------------------------------------


def _dm_handler(requests: list[httpx.Request], *, post_status: int = 200):
    """Route the two send_dm calls: open the DM channel, then post the message."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/@me/channels"):
            return httpx.Response(200, text='{"id": "DM1"}')
        return httpx.Response(post_status, text='{"id": "M7"}')

    return handle


async def test_send_dm_opens_channel_then_posts():
    requests: list[httpx.Request] = []
    message_id = await _routed_emitter(_dm_handler(requests)).send_dm("U9", "hey")
    assert message_id == "M7"
    # First the DM-channel open, then the post into the resolved channel.
    open_req, post_req = requests
    assert open_req.method == "POST"
    assert open_req.url.path.endswith("/users/@me/channels")
    assert json.loads(open_req.content) == {"recipient_id": "U9"}
    assert post_req.method == "POST"
    assert post_req.url.path.endswith("/channels/DM1/messages")
    payload = json.loads(post_req.content)
    assert payload["content"] == "hey"
    # Same embed-suppression + mention rail as create_message.
    assert payload["flags"] == 1 << 2
    assert payload["allowed_mentions"] == {"parse": ["users"]}


async def test_send_dm_truncates_to_discord_limit():
    requests: list[httpx.Request] = []
    await _routed_emitter(_dm_handler(requests)).send_dm("U9", "x" * 3000)
    post_req = requests[1]
    assert len(json.loads(post_req.content)["content"]) == 2000


async def test_send_dm_caches_dm_channel_per_fire():
    requests: list[httpx.Request] = []
    emitter = _routed_emitter(_dm_handler(requests))
    await emitter.send_dm("U9", "first")
    await emitter.send_dm("U9", "second")
    # Two sends to the same user open the channel ONCE (cache), then post twice.
    opens = [r for r in requests if r.url.path.endswith("/users/@me/channels")]
    posts = [r for r in requests if r.url.path.endswith("/channels/DM1/messages")]
    assert len(opens) == 1
    assert len(posts) == 2


async def test_send_dm_returns_false_on_403_dms_closed():
    requests: list[httpx.Request] = []
    result = await _routed_emitter(
        _dm_handler(requests, post_status=403)
    ).send_dm("U9", "hi")
    assert result is False


async def test_send_dm_returns_false_on_404_unknown_user():
    def handle(request: httpx.Request) -> httpx.Response:
        # The DM-channel open itself 404s an unknown user id.
        return httpx.Response(404, text="unknown user")

    result = await _routed_emitter(handle).send_dm("U9", "hi")
    assert result is False


async def test_send_dm_raises_on_5xx():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _routed_emitter(
            _dm_handler(requests, post_status=500)
        ).send_dm("U9", "hi")


# -- get_role_members (E5) --------------------------------------------------


def _member(
    user_id: str,
    roles: list[str],
    *,
    nick: str | None = None,
    global_name: str | None = None,
    username: str = "user",
    joined_at: str | None = "2026-01-01T00:00:00+00:00",
    avatar: str | None = None,
    guild_avatar: str | None = None,
    pending: bool = False,
) -> dict:
    """A raw Discord guild-member object as GET /guilds/{id}/members returns."""
    return {
        "user": {
            "id": user_id,
            "username": username,
            "global_name": global_name,
            "avatar": avatar,
        },
        "nick": nick,
        "roles": roles,
        "joined_at": joined_at,
        "avatar": guild_avatar,
        "pending": pending,
    }


def _members_paging_handler(
    pages_by_after: dict[str, list[dict]], requests: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve GET /guilds/G1/members keyed by the ``after`` cursor param."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        after = request.url.params.get("after", "0")
        return httpx.Response(200, text=json.dumps(pages_by_after.get(after, [])))

    return handle


async def test_get_role_members_filters_by_role_and_flattens_shape():
    requests: list[httpx.Request] = []
    page = [
        _member("100", ["R1", "R2"], nick="Nicky", username="alice", pending=True),
        _member("200", ["R2"], username="bob"),  # lacks R1 -> excluded
        _member("300", ["R1"], global_name="Cee", username="carol", avatar="a_hash"),
    ]
    handler = _members_paging_handler({"0": page, "300": []}, requests)
    members = await _routed_emitter(handler).get_role_members("R1")

    assert [m["member_id"] for m in members] == ["100", "300"]
    assert members[0] == {
        "member_id": "100",
        "display_name": "Nicky",  # nick wins
        "username": "alice",
        "joined_at": "2026-01-01T00:00:00+00:00",
        "account_created_at": _snowflake_created_at("100"),
        "has_custom_avatar": False,
        "pending": True,
    }
    # global_name fallback + custom avatar detection
    assert members[1]["display_name"] == "Cee"
    assert members[1]["has_custom_avatar"] is True
    assert members[1]["pending"] is False


async def test_get_role_members_pages_with_after_cursor():
    requests: list[httpx.Request] = []
    pages = {
        "0": [_member("10", ["R1"]), _member("20", ["R1"])],
        "20": [_member("30", ["R1"])],
        "30": [],
    }
    members = await _routed_emitter(
        _members_paging_handler(pages, requests)
    ).get_role_members("R1")

    assert [m["member_id"] for m in members] == ["10", "20", "30"]
    # after advances to the last user id of each page (0 -> 20 -> 30 -> stop).
    afters = [r.url.params.get("after") for r in requests]
    assert afters == ["0", "20", "30"]
    assert requests[0].url.params.get("limit") == "1000"


async def test_get_role_members_hard_caps_at_two_hundred():
    requests: list[httpx.Request] = []
    # One page of 250 holders: the emitter slices to 200 and does not page again.
    page = [_member(str(1000 + i), ["R1"]) for i in range(250)]
    members = await _routed_emitter(
        _members_paging_handler({"0": page}, requests)
    ).get_role_members("R1")

    assert len(members) == 200
    assert len(requests) == 1  # capped mid-page, no further fetch


async def test_get_role_members_empty_guild_returns_empty_list():
    requests: list[httpx.Request] = []
    members = await _routed_emitter(
        _members_paging_handler({"0": []}, requests)
    ).get_role_members("R1")
    assert members == []


async def test_get_role_members_raises_on_rest_error():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="no perms").get_role_members("R1")
