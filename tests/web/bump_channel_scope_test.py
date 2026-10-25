"""The Disboard bump tracker runs in the policy's bump channels only (#242).

Its own ``channel_ids`` are not read: with no policy, or a policy naming no
bump channel, it is dispatched nowhere and a fire that arrives anyway (a
reminder armed earlier) does nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis as fakeredis_aioredis
import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import smarter_dev.web.admin_handlers_jobs as admin_handlers_jobs
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.shared.database import Base
from smarter_dev.web import handler_dispatch as dispatch_module
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.api_native import admin_handlers as admin_handlers_module
from smarter_dev.web.api_native import handlers as handlers_module
from smarter_dev.web.api_native.admin_handlers import AdminHandlerController
from smarter_dev.web.api_native.handlers import HandlerController
from smarter_dev.web.bump_channel_scope import admin_scope
from smarter_dev.web.bump_channel_scope import bump_channel_ids
from smarter_dev.web.handler_dispatch import dispatch_handler_event
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import HandlerRun
from smarter_dev.web.models import ModerationPolicy
from smarter_dev.web.moderation_policy_store import save_policy

GUILD = "644299523686006834"
BUMP = "702178352395583498"
OLD = "702178352395583499"


@pytest.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


def _tracker(**over) -> AdminHandler:
    values = {
        "id": uuid4(),
        "guild_id": GUILD,
        "name": "disboard-bump-tracker",
        "trigger_type": "message",
        "settings": {"include_bot_messages": True},
        # The handler's own copy of the channel, which is no longer read.
        "channel_ids": [OLD],
        "description": "d",
        "script": "pass\n",
        "created_by_admin": "A1",
        "extension_handler_key": "bump-tracker",
    }
    values.update(over)
    return AdminHandler(**values)


async def _policy(session, channels: list[str]) -> None:
    document = mp.default_document().as_json()
    document["rules"]["bump_channel"]["channels"] = channels
    await save_policy(
        session, GUILD, document, base_revision=None, by="test", source="admin"
    )


# -- the scope ----------------------------------------------------------------


async def test_without_a_policy_the_tracker_has_no_channel(session):
    tracker = _tracker()
    session.add(tracker)
    await session.commit()

    assert await bump_channel_ids(session, GUILD) == []
    assert await admin_scope(session, tracker) == ([], False)


async def test_a_policy_without_bump_channels_gives_no_channel(session):
    tracker = _tracker()
    session.add(tracker)
    await _policy(session, [])

    assert await admin_scope(session, tracker) == ([], False)


async def test_the_policy_bump_channels_are_the_scope_whatever_the_mode(session):
    # The rule is off in the default document: the mode decides whether
    # members' chatter is removed, not where bumps are scored.
    tracker = _tracker()
    session.add(tracker)
    await _policy(session, [BUMP])

    assert await admin_scope(session, tracker) == ([BUMP], False)


async def test_a_renamed_install_row_is_still_the_tracker(session):
    tracker = _tracker(name="bumps")
    session.add(tracker)
    await _policy(session, [BUMP])

    assert await admin_scope(session, tracker) == ([BUMP], False)


async def test_other_handlers_keep_their_own_scope(session):
    other = _tracker(name="other", extension_handler_key=None, channel_ids=[])
    session.add(other)
    await _policy(session, [BUMP])

    assert await admin_scope(session, other) == ([], True)


async def test_an_unreadable_policy_stops_the_tracker_only(session):
    session.add(ModerationPolicy(guild_id=GUILD, revision=1, document={"rules": 7}))
    await session.commit()

    assert await bump_channel_ids(session, GUILD) == []


# -- dispatch -----------------------------------------------------------------


class _AllowAll:
    def __init__(self, redis=None):
        pass

    async def hit(self, key, limit):
        return True


@pytest.fixture
def submitted(monkeypatch) -> list:
    captured: list = []

    async def _submit(payload, **kwargs):
        captured.append(payload)

    monkeypatch.setattr(dispatch_module, "worker_submit", _submit)
    redis = fakeredis_aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(dispatch_module, "get_redis_client", lambda: redis)
    monkeypatch.setattr(dispatch_module, "WindowedLimiter", _AllowAll)
    return captured


async def _dispatch(session, channel_id: str) -> list[str]:
    return await dispatch_handler_event(
        session,
        guild_id=GUILD,
        channel_id=channel_id,
        trigger_type="message",
        trigger_context={"author_id": "302050872383242240", "author_is_bot": True},
    )


async def test_dispatch_fires_the_tracker_in_the_policy_channel_only(
    session, submitted
):
    tracker = _tracker()
    session.add(tracker)
    await _policy(session, [BUMP])

    assert await _dispatch(session, BUMP) == [str(tracker.id)]
    assert await _dispatch(session, OLD) == []
    assert await _dispatch(session, "C-ELSEWHERE") == []


async def test_dispatch_fires_the_tracker_nowhere_without_a_policy(session, submitted):
    session.add(_tracker())
    await session.commit()

    assert await _dispatch(session, OLD) == []
    assert await _dispatch(session, BUMP) == []
    assert submitted == []


async def test_dispatch_fires_the_tracker_nowhere_with_no_bump_channel(
    session, submitted
):
    session.add(_tracker())
    await _policy(session, [])

    assert await _dispatch(session, OLD) == []
    assert submitted == []


# -- the bot's dispatch guard ---------------------------------------------------


@pytest.fixture
def handlers_client(session) -> Iterator[TestClient]:
    original = list(handlers_module.BOT_API_GUARDS)
    handlers_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[HandlerController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(lambda: session, sync_to_thread=False)},
        ) as client:
            yield client
    finally:
        handlers_module.BOT_API_GUARDS[:] = original


async def test_active_channels_lists_the_policy_bump_channel(session, handlers_client):
    session.add(_tracker())
    await _policy(session, [BUMP])

    body = handlers_client.get("/api/handlers/active-channels").json()
    assert [BUMP, "message"] in body["channels"]
    assert [OLD, "message"] not in body["channels"]
    assert body["bot_message_channels"] == [BUMP]
    assert body["guild_triggers"] == []
    assert body["bot_message_guild_triggers"] == []


async def test_active_channels_lists_nothing_for_the_tracker_without_a_policy(
    session, handlers_client
):
    session.add(_tracker())
    await session.commit()

    body = handlers_client.get("/api/handlers/active-channels").json()
    # An empty scope is not "every channel" for the tracker.
    assert body == {
        "channels": [],
        "guild_triggers": [],
        "bot_message_channels": [],
        "bot_message_guild_triggers": [],
    }


# -- the fire job ---------------------------------------------------------------


class _SessionCtx:
    def __init__(self, engine):
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    def __call__(self):
        return self

    async def __aenter__(self):
        self._session = self._maker()
        return self._session

    async def __aexit__(self, *exc):
        await self._session.close()
        return False


async def _fire(monkeypatch, engine, handler_id, channel_id: str, trigger_type: str):
    redis = fakeredis_aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _SessionCtx(engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: redis)
    return await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id=channel_id,
            trigger_context={"trigger_type": trigger_type, "payload": {}},
        ),
        SimpleNamespace(job=SimpleNamespace(id=uuid4().hex, attempt=1, max_attempts=3)),
    )


@pytest.mark.parametrize("channels", [None, []])
async def test_a_reminder_fire_does_nothing_without_a_bump_channel(
    monkeypatch, engine, session, channels
):
    # The script would post: a fire that ran it would leave a HandlerRun.
    tracker = _tracker(trigger_type="message", script="await send_message('hi')\n")
    session.add(tracker)
    await session.commit()
    if channels is not None:
        await _policy(session, channels)

    result = await _fire(monkeypatch, engine, tracker.id, "", "timer")

    assert result == {"status": "out_of_scope"}
    runs = list(await session.scalars(select(HandlerRun)))
    assert runs == []


async def test_a_fire_from_a_channel_the_policy_dropped_does_nothing(
    monkeypatch, engine, session
):
    tracker = _tracker(script="await send_message('hi')\n")
    session.add(tracker)
    await _policy(session, [BUMP])

    result = await _fire(monkeypatch, engine, tracker.id, OLD, "message")

    assert result == {"status": "out_of_scope"}


# -- the handler API -----------------------------------------------------------


@pytest.fixture
def admin_client(session) -> Iterator[TestClient]:
    original = list(admin_handlers_module.BOT_API_GUARDS)
    admin_handlers_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[AdminHandlerController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(lambda: session, sync_to_thread=False)},
        ) as client:
            yield client
    finally:
        admin_handlers_module.BOT_API_GUARDS[:] = original


def _update(tracker, **over) -> dict:
    body = {
        "description": "d2",
        "script": "pass\n",
        "settings": {"include_bot_messages": True},
        "channel_ids": list(tracker.channel_ids),
    }
    body.update(over)
    return body


async def test_the_api_refuses_to_change_the_tracker_channels(session, admin_client):
    tracker = _tracker()
    session.add(tracker)
    await session.commit()

    response = admin_client.put(
        f"/api/admin/handlers/{tracker.id}", json=_update(tracker, channel_ids=[BUMP])
    )

    assert response.status_code == 422
    assert "moderation page" in response.json()["detail"]
    await session.refresh(tracker)
    assert tracker.channel_ids == [OLD]


async def test_the_api_refuses_to_rename_the_tracker(session, admin_client):
    tracker = _tracker()
    session.add(tracker)
    await session.commit()

    response = admin_client.put(
        f"/api/admin/handlers/{tracker.id}", json=_update(tracker, name="bumps")
    )

    assert response.status_code == 422


async def test_the_api_saves_the_tracker_with_its_channels_unchanged(
    session, admin_client
):
    tracker = _tracker()
    session.add(tracker)
    await session.commit()

    response = admin_client.put(
        f"/api/admin/handlers/{tracker.id}", json=_update(tracker)
    )

    assert response.status_code == 200, response.text
    await session.refresh(tracker)
    assert tracker.description == "d2"


async def test_the_api_refuses_a_new_tracker_with_channels(admin_client):
    response = admin_client.post(
        "/api/admin/handlers",
        json={
            "guild_id": GUILD,
            "name": "disboard-bump-tracker",
            "trigger_type": "message",
            "settings": {},
            "channel_ids": [BUMP],
            "description": "d",
            "script": "pass\n",
            "created_by_admin": "A1",
        },
    )

    assert response.status_code == 422
