"""One person's opt-out from the AI assistant (#92), on the purge block list.

Real SQLite tables. Synthetic members only: ``kai`` (111…) opts out and back
in, ``nia`` (222…) is purged.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from sqlalchemy import select

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web.api_native import privacy as privacy_module
from smarter_dev.web.api_native.privacy import PrivacyController
from smarter_dev.web.chat_bot_opt_out import opt_in
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_bot_opt_out import read_opt_out
from smarter_dev.web.chat_bot_purge import add_blocked_user
from smarter_dev.web.chat_bot_purge import read_blocked_users
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotOptIn

_KAI = "111111111111111111"
_NIA = "222222222222222222"
_OPTED_IN = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


async def _rows(db_session):
    blocked = (
        await db_session.execute(
            select(ChatBotBlockedUser.discord_user_id, ChatBotBlockedUser.source)
        )
    ).all()
    opted_in = (await db_session.scalars(select(ChatBotOptIn.discord_user_id))).all()
    return dict(blocked), list(opted_in)


async def test_opt_out_and_back_in_round_trips_and_bumps_the_revision(db_session):
    assert (await read_opt_out(db_session, _KAI)).model_dump() == {
        "opted_out": False,
        "source": None,
        "revision": 0,
    }

    state = await opt_out(db_session, _KAI)
    assert (state.opted_out, state.source, state.revision) == (True, "opt_out", 1)
    listing = await read_blocked_users(db_session)
    assert listing.user_ids == [_KAI] and listing.read_from == {}

    # Opting out twice changes nothing.
    assert (await opt_out(db_session, _KAI)).revision == 1

    state = await opt_in(db_session, _KAI, now=_OPTED_IN)
    assert (state.opted_out, state.source, state.revision) == (False, None, 2)
    listing = await read_blocked_users(db_session)
    assert listing.user_ids == []
    assert listing.read_from == {_KAI: _OPTED_IN}

    # Opting in again when not opted out changes nothing either.
    assert (await opt_in(db_session, _KAI)).revision == 2


async def test_opting_out_again_drops_the_opt_in_time(db_session):
    await opt_out(db_session, _KAI)
    await opt_in(db_session, _KAI, now=_OPTED_IN)

    await opt_out(db_session, _KAI)

    assert await _rows(db_session) == ({_KAI: "opt_out"}, [])
    assert (await read_blocked_users(db_session)).read_from == {}


async def test_opting_in_never_removes_a_purge_row(db_session):
    await add_blocked_user(db_session, _NIA)

    state = await opt_in(db_session, _NIA)

    assert (state.opted_out, state.source, state.revision) == (True, "purge", 1)
    assert await _rows(db_session) == ({_NIA: "purge"}, [])
    # And opting out on top of a purge leaves it a purge.
    assert (await opt_out(db_session, _NIA)).source == "purge"


async def test_a_purge_of_someone_opted_out_makes_the_block_permanent(db_session):
    await opt_out(db_session, _NIA)

    revision = await add_blocked_user(db_session, _NIA)

    assert revision == 1  # membership did not change
    state = await opt_in(db_session, _NIA)
    assert (state.opted_out, state.source) == (True, "purge")
    assert (await read_blocked_users(db_session)).user_ids == [_NIA]


async def test_a_purge_of_someone_opted_back_in_drops_their_opt_in_time(db_session):
    await opt_out(db_session, _NIA)
    await opt_in(db_session, _NIA, now=_OPTED_IN)

    await add_blocked_user(db_session, _NIA)

    assert await _rows(db_session) == ({_NIA: "purge"}, [])


@pytest.fixture
def unguarded_client(test_engine, db_session):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    original = list(privacy_module.BOT_API_GUARDS)
    privacy_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[PrivacyController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(session)},
        ) as client:
            yield client
    finally:
        privacy_module.BOT_API_GUARDS.extend(original)


def test_the_api_reads_sets_and_clears_with_the_id_in_the_body(unguarded_client):
    client = unguarded_client
    read = client.post("/api/privacy/opt-out/state", json={"discord_user_id": _KAI})
    assert read.status_code == 200
    assert read.json()["opted_out"] is False

    changed = client.put(
        "/api/privacy/opt-out", json={"discord_user_id": _KAI, "opted_out": True}
    )
    assert changed.status_code == 200
    assert changed.json() == {"opted_out": True, "source": "opt_out", "revision": 1}
    assert client.get("/api/privacy/blocked-users").json()["user_ids"] == [_KAI]

    cleared = client.put(
        "/api/privacy/opt-out", json={"discord_user_id": _KAI, "opted_out": False}
    )
    assert cleared.json() == {"opted_out": False, "source": None, "revision": 2}
    listing = client.get("/api/privacy/blocked-users").json()
    assert listing["user_ids"] == [] and list(listing["read_from"]) == [_KAI]


@pytest.mark.parametrize(
    "body",
    [
        {"discord_user_id": "not-an-id", "opted_out": True},
        {"discord_user_id": _KAI},
        {"opted_out": True},
    ],
    ids=["bad-id", "missing-flag", "missing-id"],
)
def test_the_api_refuses_a_malformed_change(unguarded_client, body):
    # The bot API answers validation errors as 422 (BOT_API_EXCEPTION_HANDLERS).
    assert unguarded_client.put("/api/privacy/opt-out", json=body).status_code == 422
    listing = unguarded_client.get("/api/privacy/blocked-users").json()
    assert listing == {"revision": 0, "user_ids": [], "read_from": {}}
