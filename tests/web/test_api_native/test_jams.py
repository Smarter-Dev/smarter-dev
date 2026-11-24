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
            route_handlers=[JamMessageController],
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
