"""Tests for the bot API that claims and reports scheduled jam messages."""

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
    assert GuildJamController.get_guild_jam.guards is guards


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
        "channels": {"news": None, "chat": None, "teams": None, "showcase": None},
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
        "teams": "13",
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
    assert body["channels"]["teams"] == "13"
