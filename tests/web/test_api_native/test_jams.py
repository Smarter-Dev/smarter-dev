"""Tests for the bot API for code jams: messages, teams and entries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from skrift.auth.guards import APIKeyOnly

from smarter_dev.web.api_native import jams as jams_module
from smarter_dev.web.api_native.jams import GuildJamController
from smarter_dev.web.api_native.jams import JamMessageController
from smarter_dev.web.jams import store
from tests.web.jam_helpers import GUILD
from tests.web.jam_helpers import make_jam
from tests.web.jam_helpers import make_message
from tests.web.jam_helpers import make_setup


@pytest.fixture
def client(db_session) -> Iterator[TestClient]:
    original = list(jams_module.BOT_API_GUARDS)
    jams_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[JamMessageController, GuildJamController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            yield test_client
    finally:
        jams_module.BOT_API_GUARDS[:] = original


async def _seed_due(db_session, **message):
    jam = make_jam()
    db_session.add_all([jam, make_setup()])
    await db_session.commit()
    row = make_message(jam, send_at=datetime.now(UTC) - timedelta(minutes=1), **message)
    db_session.add(row)
    await db_session.commit()
    return row


def test_every_route_carries_the_bot_api_guards():
    guards = jams_module.BOT_API_GUARDS
    assert any(isinstance(guard, APIKeyOnly) for guard in guards)
    assert len(guards) == 3
    for name in (
        "claim_due_jam_messages",
        "mark_jam_message_sent",
        "mark_jam_message_failed",
    ):
        assert getattr(JamMessageController, name).guards is guards
    for name in (
        "get_guild_jam",
        "get_jam_member",
        "create_jam_team",
        "add_jam_team_member",
        "leave_jam_team",
        "record_jam_submission",
        "remove_jam_submission",
    ):
        assert getattr(GuildJamController, name).guards is guards
    routes = [
        value
        for controller in (JamMessageController, GuildJamController)
        for value in vars(controller).values()
        if hasattr(value, "fn")
    ]
    assert len(routes) == 10
    assert all(route.guards is guards for route in routes)


async def test_claim_hands_each_message_out_once(client, db_session):
    message = await _seed_due(db_session, mention_role=True)

    first = client.post("/api/jam-messages/claim")
    second = client.post("/api/jam-messages/claim")

    assert first.status_code == 200
    assert first.json() == {
        "messages": [
            {
                "id": str(message.id),
                "guild_id": GUILD,
                "channel_id": "11",
                "content": "The jam is open!",
                "role_id": "20",
            }
        ]
    }
    assert second.json() == {"messages": []}


async def test_mark_sent_after_a_claim(client, db_session):
    message = await _seed_due(db_session)
    client.post("/api/jam-messages/claim")

    response = client.post(f"/api/jam-messages/{message.id}/mark-sent")

    assert (response.status_code, response.json()) == (200, {"success": True})
    await db_session.refresh(message)
    assert message.status == "sent"


async def test_mark_sent_without_a_claim_is_404(client, db_session):
    message = await _seed_due(db_session)

    response = client.post(f"/api/jam-messages/{message.id}/mark-sent")

    assert response.status_code == 404
    assert response.json() == {"detail": "Jam message not found or not claimed"}
    assert client.post(f"/api/jam-messages/{uuid4()}/mark-sent").status_code == 404


async def test_mark_failed_keeps_the_reason(client, db_session):
    message = await _seed_due(db_session)
    client.post("/api/jam-messages/claim")

    response = client.post(
        f"/api/jam-messages/{message.id}/mark-failed",
        json={"error": "Missing Access"},
    )

    assert (response.status_code, response.json()) == (200, {"success": True})
    await db_session.refresh(message)
    assert (message.status, message.error) == ("failed", "Missing Access")
    assert await store.retry_message(db_session, message)


async def test_mark_failed_needs_a_reason(client, db_session):
    message = await _seed_due(db_session)
    client.post("/api/jam-messages/claim")

    for body in ({}, {"error": ""}):
        response = client.post(f"/api/jam-messages/{message.id}/mark-failed", json=body)
        assert response.status_code in (400, 422)
    await db_session.refresh(message)
    assert message.status == "sending"


def test_a_malformed_id_is_rejected(client):
    assert client.post("/api/jam-messages/not-a-uuid/mark-sent").status_code in (
        400,
        422,
    )


# -- what the bot reads about a guild's jam ------------------------------------


def test_guild_jam_is_empty_before_anything_is_set_up(client):
    response = client.get(f"/api/guilds/{GUILD}/jam")

    assert response.status_code == 200
    assert response.json() == {
        "channels": {"news": None, "chat": None, "showcase": None},
        "role_id": None,
        "jam": None,
    }


async def test_guild_jam_gives_channels_role_and_the_open_jam(client, db_session):
    now = datetime.now(UTC)
    jam = make_jam(
        starts_at=now - timedelta(days=1),
        submissions_close_at=now + timedelta(days=30),
        showcase_tag_id="31",
        year_tag_id="32",
    )
    db_session.add_all([jam, make_setup()])
    await db_session.commit()

    body = client.get(f"/api/guilds/{GUILD}/jam").json()

    assert body["channels"] == {
        "news": "11",
        "chat": "12",
        "showcase": "14",
    }
    assert body["role_id"] == "20"
    assert body["jam"]["id"] == str(jam.id)
    assert body["jam"]["name"] == "Holiday Chatbot Jam"
    assert body["jam"]["max_team_size"] == 4
    assert body["jam"]["showcase_tag_id"] == "31"
    assert body["jam"]["year_tag_id"] == "32"
    # Times carry their offset, so the bot compares them with aware times.
    closes = datetime.fromisoformat(body["jam"]["submissions_close_at"])
    assert closes.utcoffset() == timedelta(0)
    assert abs(closes - (now + timedelta(days=30))) < timedelta(seconds=1)
    assert datetime.fromisoformat(body["jam"]["starts_at"]).utcoffset() == timedelta(0)


async def test_guild_jam_keeps_the_channels_between_jams(client, db_session):
    now = datetime.now(UTC)
    over = make_jam(
        starts_at=now - timedelta(days=40),
        submissions_close_at=now - timedelta(days=1),
    )
    db_session.add_all([over, make_setup()])
    await db_session.commit()

    body = client.get(f"/api/guilds/{GUILD}/jam").json()

    assert body["jam"] is None
    assert body["role_id"] == "20"
    assert body["channels"]["chat"] == "12"


# -- teams and entries --------------------------------------------------------

JAM = f"/api/guilds/{GUILD}/jam"


async def _open_jam(db_session, **overrides):
    now = datetime.now(UTC)
    values = {
        "starts_at": now - timedelta(days=1),
        "submissions_close_at": now + timedelta(days=30),
    }
    values.update(overrides)
    jam = make_jam(**values)
    db_session.add_all([jam, make_setup()])
    await db_session.commit()
    return jam


def _make_team(client, thread_id="500", user_id="1", name="Elves") -> dict:
    response = client.post(
        f"{JAM}/teams", json={"name": name, "thread_id": thread_id, "user_id": user_id}
    )
    assert response.status_code == 200
    return response.json()


def _invite(client, user_id, by="1", thread_id="500") -> dict:
    response = client.post(
        f"{JAM}/teams/{thread_id}/members",
        json={"user_id": user_id, "invited_by": by},
    )
    assert response.status_code == 200
    return response.json()


def _post_entry(client, thread_id, author_id) -> dict:
    response = client.post(
        f"{JAM}/submissions", json={"thread_id": thread_id, "author_id": author_id}
    )
    assert response.status_code == 200
    return response.json()


async def test_a_team_is_made_joined_and_left(client, db_session):
    await _open_jam(db_session)

    made = _make_team(client)
    added = _invite(client, "2")
    member = client.get(f"{JAM}/members/2").json()
    left = client.post(f"{JAM}/teams/500/leave", json={"user_id": "2"}).json()

    assert made == {
        "ok": True,
        "reason": None,
        "team": {"name": "Elves", "thread_id": "500", "member_ids": ["1"]},
        "already": False,
        "members_left": 0,
    }
    assert added["ok"] is True and added["already"] is False
    assert member == {
        "ok": False,
        "reason": "on_a_team",
        "team": {"name": "Elves", "thread_id": "500", "member_ids": ["1", "2"]},
    }
    assert left["ok"] is True and left["members_left"] == 1
    assert client.get(f"{JAM}/members/2").json() == {
        "ok": True,
        "reason": None,
        "team": None,
    }


async def test_a_refused_team_change_is_a_200_that_says_why(client, db_session):
    """The bot's API client raises on any 4xx and reads no body from it."""
    await _open_jam(db_session, max_team_size=2)
    _make_team(client)
    _make_team(client, thread_id="501", user_id="3")

    assert _make_team(client, thread_id="502")["reason"] == "on_a_team"
    assert _invite(client, "3")["reason"] == "on_a_team"
    assert _invite(client, "4", by="9")["reason"] == "not_your_team"
    assert _invite(client, "4", thread_id="999")["reason"] == "not_a_team"
    assert _invite(client, "2")["ok"] is True
    assert _invite(client, "4")["reason"] == "full"


async def test_no_team_before_the_jam_starts(client, db_session):
    now = datetime.now(UTC)
    await _open_jam(db_session, starts_at=now + timedelta(days=7))

    assert client.get(f"{JAM}/members/1").json()["reason"] == "not_open"
    assert _make_team(client)["reason"] == "not_open"
    assert _post_entry(client, "700", "1")["reason"] == "no_jam"


async def test_team_routes_say_no_jam_between_jams(client, db_session):
    now = datetime.now(UTC)
    await _open_jam(
        db_session,
        starts_at=now - timedelta(days=40),
        submissions_close_at=now - timedelta(days=1),
    )
    no_jam = {"ok": False, "reason": "no_jam", "team": None}

    assert client.get(f"{JAM}/members/1").json() == no_jam
    assert _make_team(client) == no_jam
    assert _invite(client, "2") == no_jam
    assert client.post(f"{JAM}/teams/500/leave", json={"user_id": "1"}).json() == no_jam
    assert _post_entry(client, "700", "1") == no_jam


async def test_a_showcase_post_is_recorded_against_the_team(client, db_session):
    await _open_jam(db_session)
    _make_team(client)
    _invite(client, "2")

    first = _post_entry(client, "700", "2")
    second = _post_entry(client, "701", "1")
    again = _post_entry(client, "701", "1")
    solo = _post_entry(client, "702", "9")

    assert first == {
        "ok": True,
        "reason": None,
        "team": {"name": "Elves", "thread_id": "500", "member_ids": ["1", "2"]},
        "entry_thread_id": "700",
        "new": True,
    }
    assert (second["entry_thread_id"], second["new"]) == ("700", True)
    assert (again["entry_thread_id"], again["new"]) == ("700", False)
    assert solo["team"] is None and solo["entry_thread_id"] == "702"
    assert _invite(client, "3")["reason"] == "team_has_entry"
    left = client.post(f"{JAM}/teams/500/leave", json={"user_id": "2"}).json()
    assert left["reason"] == "team_has_entry"
    assert _make_team(client, thread_id="501", user_id="9")["reason"] == "has_entry"


async def test_a_deleted_post_is_forgotten(client, db_session):
    await _open_jam(db_session)
    _make_team(client)
    _post_entry(client, "700", "1")

    removed = client.post(f"{JAM}/submissions/700/remove").json()
    again = client.post(f"{JAM}/submissions/700/remove").json()

    assert (removed, again) == ({"removed": True}, {"removed": False})
    assert client.post(f"{JAM}/teams/500/leave", json={"user_id": "1"}).json()["ok"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("teams", {"name": "", "thread_id": "500", "user_id": "1"}),
        ("teams", {"name": "x" * 101, "thread_id": "500", "user_id": "1"}),
        ("teams", {"name": "Elves", "thread_id": "abc", "user_id": "1"}),
        ("teams", {"name": "Elves", "thread_id": "500", "user_id": "1 OR 1"}),
        ("teams/500/members", {"user_id": "2"}),
        ("teams/500/leave", {}),
        ("submissions", {"thread_id": "700", "author_id": ""}),
    ],
)
async def test_malformed_team_requests_are_rejected(client, db_session, path, body):
    await _open_jam(db_session)

    assert client.post(f"{JAM}/{path}", json=body).status_code == 422
