"""Parity tests for the native (Litestar) channel-handlers controller.

Port of the FastAPI suite ``tests/web/test_api/test_handlers.py`` against
``smarter_dev.web.api_native.handlers`` — same in-memory SQLite database, same
stubbed worker/limiter seams, same status codes and JSON bodies, with the
final ``/api/handlers`` paths the bot client sends.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web.api_native import handlers as handlers_module
from smarter_dev.web.api_native.handlers import HandlerController
from smarter_dev.web.handler_caps import MAX_HANDLERS_PER_CHANNEL


class _StubLimiter:
    def __init__(self, redis=None, allow=True):
        self.allow = allow

    async def hit(self, key, limit):
        return self.allow


class _StubJobHandle:
    cancelled: list[str] = []

    def __init__(self, job_id):
        self.job_id = job_id

    async def cancel(self):
        _StubJobHandle.cancelled.append(self.job_id)


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def submitted(monkeypatch) -> list[tuple]:
    """Capture ``worker_submit`` calls and stub the scheduling/limiter seams."""
    captured: list[tuple] = []

    async def _submit(payload, **kwargs):
        captured.append((payload, kwargs))

    _StubJobHandle.cancelled = []
    monkeypatch.setattr(handlers_module, "worker_submit", _submit)
    monkeypatch.setattr(handlers_module, "get_handle", _StubJobHandle)
    monkeypatch.setattr(handlers_module, "get_redis_client", lambda: None)
    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _StubLimiter(allow=True)
    )
    return captured


@pytest.fixture
def client(db_session, submitted) -> Iterator[TestClient]:
    """Litestar client serving the handlers controller with guards bypassed.

    The routes share the ``handlers.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes the guards for these tests
    only. Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(handlers_module.BOT_API_GUARDS)
    handlers_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[HandlerController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            test_client.submitted = submitted  # type: ignore[attr-defined]
            yield test_client
    finally:
        handlers_module.BOT_API_GUARDS[:] = original_guards


def _event_body(**over):
    body = {
        "guild_id": "G1",
        "channel_id": "C1",
        "name": "huzzah-reactor",
        "trigger_type": "message",
        "settings": {},
        "description": "react on huzzah",
        "script": 'await add_reaction(context["message_id"], "🎉")\n',
        "created_by": "U1",
    }
    body.update(over)
    return body


def test_create_event_handler(client):
    resp = client.post("/api/handlers", json=_event_body())
    assert resp.status_code == 201
    assert resp.json()["trigger_type"] == "message"
    assert resp.json()["name"] == "huzzah-reactor"


def test_multiple_handlers_per_trigger_coexist(client):
    first = client.post("/api/handlers", json=_event_body(name="greeter"))
    second = client.post("/api/handlers", json=_event_body(name="mood-tracker"))
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["handler_id"] != second.json()["handler_id"]
    listed = client.get("/api/handlers", params={"channel_id": "C1"})
    assert {r["name"] for r in listed.json()} == {"greeter", "mood-tracker"}


def test_duplicate_name_in_channel_is_conflict(client):
    client.post("/api/handlers", json=_event_body(name="greeter"))
    dupe = client.post(
        "/api/handlers", json=_event_body(name="greeter", trigger_type="reaction")
    )
    assert dupe.status_code == 409
    # Same name in a DIFFERENT channel is fine.
    other = client.post(
        "/api/handlers", json=_event_body(name="greeter", channel_id="C2")
    )
    assert other.status_code == 201


def test_blank_name_is_rejected(client):
    resp = client.post("/api/handlers", json=_event_body(name="   "))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "name is required"}


def test_unknown_trigger_type_is_rejected(client):
    resp = client.post("/api/handlers", json=_event_body(trigger_type="telepathy"))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "unknown trigger_type"}


def test_handler_count_cap_per_channel(client):
    for n in range(MAX_HANDLERS_PER_CHANNEL):
        resp = client.post("/api/handlers", json=_event_body(name=f"h{n}"))
        assert resp.status_code == 201
    over = client.post("/api/handlers", json=_event_body(name="one-too-many"))
    assert over.status_code == 422


def test_create_timer_schedules_first_fire(client):
    body = _event_body(
        trigger_type="timer",
        settings={"delay_seconds": 3600},
        script='await send_message("reminder")\n',
        description="remind in an hour",
    )
    resp = client.post("/api/handlers", json=body)
    assert resp.status_code == 201
    assert len(client.submitted) == 1  # type: ignore[attr-defined]
    _, kwargs = client.submitted[0]  # type: ignore[attr-defined]
    assert "scheduled_for" in kwargs and "job_id" in kwargs


def test_schedule_below_floor_is_rejected(client):
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 5},
        script='await send_message("spam")\n',
    )
    resp = client.post("/api/handlers", json=body)
    assert resp.status_code == 422


def test_schedule_start_at_is_persisted_and_used_for_first_fire(client):
    start_at = "2099-08-01T14:30:00Z"
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 3600, "start_at": start_at},
        script='await send_message("hourly")\n',
    )
    resp = client.post("/api/handlers", json=body)
    assert resp.status_code == 201
    assert resp.json()["settings"]["start_at"] == start_at
    _, kwargs = client.submitted[0]  # type: ignore[attr-defined]
    assert kwargs["scheduled_for"] == datetime(2099, 8, 1, 14, 30, tzinfo=UTC)


def test_schedule_start_at_requires_explicit_utc(client):
    body = _event_body(
        trigger_type="schedule",
        settings={
            "interval_seconds": 3600,
            "start_at": "2099-08-01T14:30:00",
        },
    )
    resp = client.post("/api/handlers", json=body)
    assert resp.status_code == 422
    assert "explicit UTC offset" in resp.json()["detail"]


def test_list_and_delete(client):
    client.post("/api/handlers", json=_event_body())
    listed = client.get("/api/handlers", params={"channel_id": "C1"})
    assert len(listed.json()) == 1
    assert "script" not in listed.json()[0]
    handler_id = listed.json()[0]["handler_id"]
    deleted = client.delete(f"/api/handlers/{handler_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": handler_id}
    again = client.get("/api/handlers", params={"channel_id": "C1"})
    assert again.json() == []


def test_list_requires_channel_id(client):
    resp = client.get("/api/handlers")
    assert resp.status_code == 422


def test_list_with_scripts(client):
    client.post("/api/handlers", json=_event_body())
    listed = client.get(
        "/api/handlers", params={"channel_id": "C1", "include_scripts": "true"}
    )
    assert listed.json()[0]["script"].startswith("await add_reaction")


def test_edit_handler_updates_script_and_description(client):
    created = client.post("/api/handlers", json=_event_body())
    handler_id = created.json()["handler_id"]
    resp = client.put(
        f"/api/handlers/{handler_id}",
        json={
            "description": "react on hooray too",
            "script": 'await add_reaction(context["message_id"], "🎊")\n',
            "settings": {},
        },
    )
    assert resp.status_code == 200
    detail = client.get(f"/api/handlers/{handler_id}")
    assert detail.json()["description"] == "react on hooray too"
    assert "🎊" in detail.json()["script"]
    assert detail.json()["name"] == "huzzah-reactor"  # unchanged without rename


def test_edit_can_rename_but_not_to_taken_name(client):
    client.post("/api/handlers", json=_event_body(name="greeter"))
    created = client.post("/api/handlers", json=_event_body(name="mood"))
    handler_id = created.json()["handler_id"]
    renamed = client.put(
        f"/api/handlers/{handler_id}",
        json={"description": "d", "script": "pass\n", "settings": {}, "name": "vibes"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "vibes"
    collision = client.put(
        f"/api/handlers/{handler_id}",
        json={"description": "d", "script": "pass\n", "settings": {}, "name": "greeter"},
    )
    assert collision.status_code == 409


def test_edit_time_handler_cancels_and_reschedules(client):
    body = _event_body(
        trigger_type="schedule",
        settings={"interval_seconds": 3600},
        script='await send_message("hourly")\n',
    )
    created = client.post("/api/handlers", json=body)
    handler_id = created.json()["handler_id"]
    assert len(client.submitted) == 1  # type: ignore[attr-defined]

    resp = client.put(
        f"/api/handlers/{handler_id}",
        json={
            "description": "every two hours",
            "script": 'await send_message("bihourly")\n',
            "settings": {"interval_seconds": 7200},
        },
    )
    assert resp.status_code == 200
    assert len(_StubJobHandle.cancelled) == 1
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


def test_edit_unknown_handler_is_404(client):
    resp = client.put(
        "/api/handlers/00000000-0000-0000-0000-000000000000",
        json={"description": "d", "script": "pass\n", "settings": {}},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "handler not found"}


def test_malformed_handler_id_is_422(client):
    resp = client.put(
        "/api/handlers/not-a-uuid",
        json={"description": "d", "script": "pass\n", "settings": {}},
    )
    assert resp.status_code == 422


def test_dispatch_no_handler(client):
    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {},
        },
    )
    assert resp.json()["dispatched"] is False


def test_dispatch_fires_all_standard_handlers_for_trigger(client):
    client.post("/api/handlers", json=_event_body(name="greeter"))
    client.post("/api/handlers", json=_event_body(name="mood-tracker"))
    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"trigger_type": "message", "message_content": "huzzah"},
        },
    )
    assert resp.json()["dispatched"] is True
    assert len(resp.json()["handler_ids"]) == 2
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


def test_dispatch_rate_limited(client, monkeypatch):
    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _StubLimiter(allow=False)
    )
    client.post("/api/handlers", json=_event_body())
    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {},
        },
    )
    assert resp.json()["dispatched"] is False


async def test_dispatch_fans_out_to_standard_and_admin(client, db_session):
    from smarter_dev.web.models import AdminHandler, ChannelHandler

    # one standard handler in C1, one all-channel admin handler, one admin
    # handler scoped to a different channel (should NOT fire for C1).
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="std", trigger_type="message",
        settings={}, description="std", script="await send_message('x')\n",
        created_by="U1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="all-chan", trigger_type="message", settings={},
        channel_ids=[], description="all-chan admin",
        script="await send_message('y')\n", created_by_admin="A1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="scoped", trigger_type="message", settings={},
        channel_ids=["OTHER"], description="scoped admin",
        script="await send_message('z')\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    # standard + all-channel admin = 2 fires; the OTHER-scoped admin is skipped.
    assert len(body["handler_ids"]) == 2
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


def test_active_channels(client):
    client.post("/api/handlers", json=_event_body())
    client.post("/api/handlers", json=_event_body(name="rx", trigger_type="reaction"))
    resp = client.get("/api/handlers/active-channels")
    channels = resp.json()["channels"]
    assert ["C1", "message"] in channels
    assert ["C1", "reaction"] in channels


def test_create_handler_rejects_include_bot_messages_on_non_message(client):
    # include_bot_messages only means anything on a message trigger.
    rejected = client.post(
        "/api/handlers",
        json=_event_body(
            name="rx", trigger_type="reaction",
            settings={"include_bot_messages": True},
        ),
    )
    assert rejected.status_code == 422
    # ... but a message-trigger handler accepts it.
    accepted = client.post(
        "/api/handlers",
        json=_event_body(name="botwatch", settings={"include_bot_messages": True}),
    )
    assert accepted.status_code == 201


async def test_dispatch_bot_message_fires_only_optin_handlers(client, db_session):
    from smarter_dev.web.models import AdminHandler, ChannelHandler

    # A plain message handler + an opted-in one, both in C1. A bot-authored
    # message (author_is_bot) must fire ONLY the opted-in handler.
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="plain", trigger_type="message",
        settings={}, description="plain", script="await send_message('x')\n",
        created_by="U1",
    ))
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="botwatch", trigger_type="message",
        settings={"include_bot_messages": True}, description="opt-in",
        script="await send_message('y')\n", created_by="U1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="disboard", trigger_type="message",
        settings={"include_bot_messages": True}, channel_ids=["C1"],
        description="tracker", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"author_is_bot": True, "author_id": "BOT9"},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    # The opted-in standard handler + the opted-in admin handler; NOT the plain one.
    assert len(body["handler_ids"]) == 2
    assert len(client.submitted) == 2  # type: ignore[attr-defined]


async def test_dispatch_bot_message_records_no_member_activity(client, db_session):
    from sqlalchemy import select

    from smarter_dev.web.models import ChannelHandler, MemberActivity

    # Activity recording is human-only: an opted-in handler firing on a
    # bot-authored message must NOT upsert a MemberActivity row for the bot's id
    # (nor hand the handler human-shaped activity facts).
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="botwatch", trigger_type="message",
        settings={"include_bot_messages": True}, description="opt-in",
        script="await send_message('y')\n", created_by="U1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"author_is_bot": True, "author_id": "BOT9"},
        },
    )
    assert resp.json()["dispatched"] is True
    rows = (await db_session.execute(
        select(MemberActivity).where(MemberActivity.user_id == "BOT9")
    )).scalars().all()
    assert rows == []


async def test_dispatch_human_message_fires_all_message_handlers(client, db_session):
    from smarter_dev.web.models import ChannelHandler

    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="plain", trigger_type="message",
        settings={}, description="plain", script="await send_message('x')\n",
        created_by="U1",
    ))
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="botwatch", trigger_type="message",
        settings={"include_bot_messages": True}, description="opt-in",
        script="await send_message('y')\n", created_by="U1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"author_id": "U7"},  # author_is_bot absent = human
        },
    )
    # Both fire on a human message — the opt-in never suppresses human traffic.
    assert len(resp.json()["handler_ids"]) == 2


async def test_active_channels_includes_bot_message_channels(client, db_session):
    from smarter_dev.web.models import AdminHandler, ChannelHandler

    # standard opt-in -> channel entry
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="botwatch", trigger_type="message",
        settings={"include_bot_messages": True}, description="d",
        script="pass\n", created_by="U1",
    ))
    # a plain message handler must NOT appear in the bot-message set
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C2", name="plain", trigger_type="message",
        settings={}, description="d", script="pass\n", created_by="U1",
    ))
    # admin scoped opt-in -> channel entry
    db_session.add(AdminHandler(
        guild_id="G1", name="scoped", trigger_type="message",
        settings={"include_bot_messages": True}, channel_ids=["C3"],
        description="d", script="pass\n", created_by_admin="A1",
    ))
    # admin guild-wide opt-in -> guild entry
    db_session.add(AdminHandler(
        guild_id="G1", name="wide", trigger_type="message",
        settings={"include_bot_messages": True}, channel_ids=[],
        description="d", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    body = client.get("/api/handlers/active-channels").json()
    assert "C1" in body["bot_message_channels"]
    assert "C3" in body["bot_message_channels"]
    assert "C2" not in body["bot_message_channels"]
    assert "G1" in body["bot_message_guild_triggers"]


# --- admin-only member/thread triggers (threads-and-member-events.md §3) -------


class _KeyAwareLimiter:
    """Allows every key except the per-guild member-events window.

    Simulates a raid: the ``hcap:memberevt:{guild}`` window is exhausted while
    per-handler fire windows stay open, isolating the guild gate under test.
    """

    def __init__(self, redis=None):
        self.hits: list[tuple[str, int]] = []

    async def hit(self, key, limit):
        self.hits.append((key, limit))
        return "memberevt" not in key


async def test_dispatch_member_event_matches_admin_by_guild_bypassing_scope(
    client, db_session
):
    from smarter_dev.web.models import AdminHandler

    # Scope names a specific channel, but a member event has no channel: the
    # scope check is bypassed and the handler still matches by guild + trigger.
    db_session.add(AdminHandler(
        guild_id="G1", name="join-gate", trigger_type="member_join", settings={},
        channel_ids=["SOMECHAN"], description="gate joins",
        script="await send_message('welcome', 'LOGCHAN')\n", created_by_admin="A1",
    ))
    # A handler in a DIFFERENT guild must not fire.
    db_session.add(AdminHandler(
        guild_id="G2", name="other", trigger_type="member_join", settings={},
        channel_ids=[], description="other guild", script="pass\n",
        created_by_admin="A2",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "",
            "trigger_type": "member_join",
            "trigger_context": {"trigger_type": "member_join", "member_id": "U9"},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    assert len(body["handler_ids"]) == 1
    # The fire carries no home channel — every send must name its target.
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert payload.channel_id == ""


@pytest.mark.parametrize(
    "trigger",
    ["member_join", "member_leave", "member_rules_accepted", "member_role_change"],
)
async def test_dispatch_member_events_decline_past_guild_window(
    client, db_session, monkeypatch, trigger
):
    from smarter_dev.web.models import AdminHandler

    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _KeyAwareLimiter()
    )
    db_session.add(AdminHandler(
        guild_id="G1", name="gate", trigger_type=trigger, settings={},
        channel_ids=[], description="gate", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "",
            "trigger_type": trigger,
            "trigger_context": {"trigger_type": trigger},
        },
    )
    # Guild window exhausted → declined before any fire is enqueued.
    assert resp.json()["dispatched"] is False
    assert len(client.submitted) == 0  # type: ignore[attr-defined]


async def test_dispatch_thread_create_scope_matches_parent(client, db_session):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="forum-triage", trigger_type="thread_create", settings={},
        channel_ids=["PARENT"], description="triage", script="pass\n",
        created_by_admin="A1",
    ))
    # Scoped to a different parent channel → must not fire.
    db_session.add(AdminHandler(
        guild_id="G1", name="other-forum", trigger_type="thread_create", settings={},
        channel_ids=["OTHER"], description="other", script="pass\n",
        created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "PARENT",  # bot dispatches thread_create on the parent
            "trigger_type": "thread_create",
            "trigger_context": {
                "trigger_type": "thread_create",
                "parent_channel_id": "PARENT",
            },
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    assert len(body["handler_ids"]) == 1
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert payload.channel_id == "PARENT"


async def test_dispatch_thread_create_empty_scope_matches_any_parent(
    client, db_session
):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="all-forums", trigger_type="thread_create", settings={},
        channel_ids=[], description="all", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "ANYPARENT",
            "trigger_type": "thread_create",
            "trigger_context": {"trigger_type": "thread_create"},
        },
    )
    assert resp.json()["dispatched"] is True


async def test_dispatch_thread_create_not_gated_by_member_window(
    client, db_session, monkeypatch
):
    from smarter_dev.web.models import AdminHandler

    # Even with the member-events window exhausted, thread_create is unaffected —
    # it is bounded by the per-handler fire cap and the thread-op caps, not this.
    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _KeyAwareLimiter()
    )
    db_session.add(AdminHandler(
        guild_id="G1", name="all-forums", trigger_type="thread_create", settings={},
        channel_ids=[], description="all", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "ANYPARENT",
            "trigger_type": "thread_create",
            "trigger_context": {"trigger_type": "thread_create"},
        },
    )
    assert resp.json()["dispatched"] is True


async def test_active_channels_member_event_always_guild_scoped(client, db_session):
    from smarter_dev.web.models import AdminHandler

    # A member_join handler surfaces as a guild trigger even when channel_ids is
    # set — its dispatch guard is per-guild (a member event has no channel).
    db_session.add(AdminHandler(
        guild_id="G1", name="join-gate", trigger_type="member_join", settings={},
        channel_ids=["SOMECHAN"], description="gate", script="pass\n",
        created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.get("/api/handlers/active-channels")
    body = resp.json()
    assert ["G1", "member_join"] in body["guild_triggers"]
    assert ["SOMECHAN", "member_join"] not in body["channels"]


async def test_active_channels_thread_create_follows_channel_split(client, db_session):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="scoped-forum", trigger_type="thread_create", settings={},
        channel_ids=["PARENT"], description="s", script="pass\n", created_by_admin="A1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="wide-forum", trigger_type="thread_create", settings={},
        channel_ids=[], description="w", script="pass\n", created_by_admin="A2",
    ))
    await db_session.commit()

    resp = client.get("/api/handlers/active-channels")
    body = resp.json()
    assert ["PARENT", "thread_create"] in body["channels"]
    assert ["G1", "thread_create"] in body["guild_triggers"]


@pytest.mark.parametrize(
    "trigger",
    [
        "member_join",
        "member_leave",
        "member_rules_accepted",
        "member_role_change",
        "thread_create",
        "message_edit",
    ],
)
def test_create_standard_rejects_admin_only_triggers(client, trigger):
    resp = client.post("/api/handlers", json=_event_body(trigger_type=trigger))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "unknown trigger_type"}


# ---------------------------------------------------------------------------
# message_edit dispatch — channel-keyed admin trigger, not raid-gated (§3.3)
# ---------------------------------------------------------------------------


async def test_dispatch_message_edit_fires_admin_handler_in_scope(client, db_session):
    from smarter_dev.web.models import AdminHandler, ChannelHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="edit-catch", trigger_type="message_edit", settings={},
        channel_ids=["EDITCHAN"], description="catch edits", script="pass\n",
        created_by_admin="A1",
    ))
    # A standard message handler in the same channel must NOT fire on an edit —
    # message_edit is admin-only, so the standard query is skipped entirely.
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="EDITCHAN", name="std", trigger_type="message",
        settings={}, description="std", script="pass\n", created_by="U1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "EDITCHAN",
            "trigger_type": "message_edit",
            "trigger_context": {"trigger_type": "message_edit", "message_id": "M1"},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    assert len(body["handler_ids"]) == 1
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert payload.channel_id == "EDITCHAN"


async def test_dispatch_message_edit_empty_scope_matches_any_channel(
    client, db_session
):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="all-edits", trigger_type="message_edit", settings={},
        channel_ids=[], description="all", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "ANYCHAN",
            "trigger_type": "message_edit",
            "trigger_context": {"trigger_type": "message_edit"},
        },
    )
    assert resp.json()["dispatched"] is True


async def test_dispatch_message_edit_respects_channel_scope(client, db_session):
    from smarter_dev.web.models import AdminHandler

    # Scoped to a DIFFERENT channel → must not fire for an edit elsewhere.
    db_session.add(AdminHandler(
        guild_id="G1", name="other-edits", trigger_type="message_edit", settings={},
        channel_ids=["OTHER"], description="other", script="pass\n",
        created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "EDITCHAN",
            "trigger_type": "message_edit",
            "trigger_context": {"trigger_type": "message_edit"},
        },
    )
    assert resp.json()["dispatched"] is False
    assert len(client.submitted) == 0  # type: ignore[attr-defined]


async def test_dispatch_message_edit_not_gated_by_member_window(
    client, db_session, monkeypatch
):
    from smarter_dev.web.models import AdminHandler

    # Even with the member-events raid window exhausted, message_edit is
    # unaffected — it is a channel-keyed trigger, not a member lifecycle event.
    monkeypatch.setattr(
        handlers_module, "WindowedLimiter", lambda redis: _KeyAwareLimiter()
    )
    db_session.add(AdminHandler(
        guild_id="G1", name="all-edits", trigger_type="message_edit", settings={},
        channel_ids=[], description="all", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "ANYCHAN",
            "trigger_type": "message_edit",
            "trigger_context": {"trigger_type": "message_edit"},
        },
    )
    assert resp.json()["dispatched"] is True


async def test_active_channels_message_edit_follows_channel_split(client, db_session):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="scoped-edit", trigger_type="message_edit", settings={},
        channel_ids=["EDITCHAN"], description="s", script="pass\n",
        created_by_admin="A1",
    ))
    db_session.add(AdminHandler(
        guild_id="G1", name="wide-edit", trigger_type="message_edit", settings={},
        channel_ids=[], description="w", script="pass\n", created_by_admin="A2",
    ))
    await db_session.commit()

    body = client.get("/api/handlers/active-channels").json()
    # Guild-wide message_edit surfaces as a guild trigger; scoped one as a channel.
    assert ["EDITCHAN", "message_edit"] in body["channels"]
    assert ["G1", "message_edit"] in body["guild_triggers"]


# ---------------------------------------------------------------------------
# dm_message dispatch — guild-scoped, no home channel, per-author window (E1)
# ---------------------------------------------------------------------------


class _DmAuthorLimiter:
    """Counts per key; only the per-(handler, author) DM window is enforced.

    A shared instance across dispatch calls lets a spammer exhaust their own
    window while every other key (fire caps, other authors) stays open.
    """

    def __init__(self, redis=None):
        self.counts: dict = {}

    async def hit(self, key, limit):
        self.counts[key] = self.counts.get(key, 0) + 1
        if "dmtrig" in key:
            return self.counts[key] <= limit
        return True


async def test_dispatch_dm_message_enqueues_admin_handler_channel_bypass(
    client, db_session
):
    from smarter_dev.web.models import AdminHandler

    # Scope names a channel, but a DM has no channel: the scope check is bypassed
    # and the handler matches by guild + trigger, fired with channel_id="".
    db_session.add(AdminHandler(
        guild_id="G1", name="dm-mirror", trigger_type="dm_message", settings={},
        channel_ids=["SOMECHAN"], description="mirror DMs",
        script="await send_message('x', 'LOG')\n", created_by_admin="A1",
    ))
    # A dm_message handler in a DIFFERENT guild must not fire.
    db_session.add(AdminHandler(
        guild_id="G2", name="other", trigger_type="dm_message", settings={},
        channel_ids=[], description="other guild", script="pass\n",
        created_by_admin="A2",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "",
            "trigger_type": "dm_message",
            "trigger_context": {"trigger_type": "dm_message", "author_id": "U9"},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    assert len(body["handler_ids"]) == 1
    # The fire carries no home channel — so its error notice can never leak into
    # the user's DM (see admin_handlers_jobs / notify_handler_error).
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert payload.channel_id == ""


async def test_dispatch_dm_message_skips_standard_tier(client, db_session):
    from smarter_dev.web.models import ChannelHandler

    # A standard message handler in the DM channel must never fire on a DM: the
    # standard query is skipped entirely for the admin-only dm_message trigger.
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="DM1", name="std", trigger_type="message",
        settings={}, description="std", script="await send_message('x')\n",
        created_by="U1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "DM1",
            "trigger_type": "dm_message",
            "trigger_context": {"trigger_type": "dm_message", "author_id": "U9"},
        },
    )
    assert resp.json()["dispatched"] is False
    assert len(client.submitted) == 0  # type: ignore[attr-defined]


async def test_dispatch_dm_per_author_window_declines_spammer(
    client, db_session, monkeypatch
):
    from smarter_dev.web.models import AdminHandler

    shared = _DmAuthorLimiter()
    monkeypatch.setattr(handlers_module, "WindowedLimiter", lambda redis: shared)
    db_session.add(AdminHandler(
        guild_id="G1", name="dm-mirror", trigger_type="dm_message", settings={},
        channel_ids=[], description="mirror", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    def _dm(author_id: str):
        return client.post(
            "/api/handlers/dispatch",
            json={
                "guild_id": "G1",
                "channel_id": "",
                "trigger_type": "dm_message",
                "trigger_context": {"trigger_type": "dm_message", "author_id": author_id},
            },
        ).json()["dispatched"]

    # DM_FIRES_PER_AUTHOR_PER_MIN = 4: the same author's first four fire, the
    # fifth is declined — they burn their OWN window, not the handler's budget.
    results = [_dm("SPAMMER") for _ in range(5)]
    assert results == [True, True, True, True, False]
    # A DIFFERENT author still fires — the window is per-(handler, author).
    assert _dm("OTHER") is True


async def test_active_channels_surfaces_dm_message_as_guild_trigger(client, db_session):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="dm-mirror", trigger_type="dm_message", settings={},
        channel_ids=[], description="mirror", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    body = client.get("/api/handlers/active-channels").json()
    # A DM has no channel, so it surfaces as a (guild_id, trigger) guild-trigger,
    # never a channel entry.
    assert ["G1", "dm_message"] in body["guild_triggers"]
    assert all(trigger != "dm_message" for _, trigger in body["channels"])


async def test_dispatch_mod_action_enqueues_guild_wide_handlers_ignoring_scope(
    client, db_session
):
    from smarter_dev.web.models import AdminHandler

    # Scope names a specific channel, but a mod_action fire has no home channel:
    # the scope check is bypassed and the handler matches by guild + trigger.
    db_session.add(AdminHandler(
        guild_id="G1", name="mod-log", trigger_type="mod_action", settings={},
        channel_ids=["SOMECHAN"], description="format audit rows",
        script="await send_message('logged', 'MODLOG')\n", created_by_admin="A1",
    ))
    # A handler in a DIFFERENT guild must not fire.
    db_session.add(AdminHandler(
        guild_id="G2", name="other", trigger_type="mod_action", settings={},
        channel_ids=[], description="other guild", script="pass\n",
        created_by_admin="A2",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "",
            "trigger_type": "mod_action",
            "trigger_context": {"trigger_type": "mod_action", "action_type": "ban"},
        },
    )
    body = resp.json()
    assert body["dispatched"] is True
    assert len(body["handler_ids"]) == 1
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert payload.channel_id == ""
    # The fire's context carries the guild id so a mod-log formatter can build a
    # "Jump To Action" link (the prompt documents context["guild_id"]).
    assert payload.trigger_context["guild_id"] == "G1"


async def test_dispatch_injects_guild_id_into_message_context(client, db_session):
    from smarter_dev.web.models import ChannelHandler

    # Every gateway-dispatched fire gets context["guild_id"] host-side, so a
    # !history-style handler can build jump links to a member's mod actions.
    db_session.add(ChannelHandler(
        guild_id="G1", channel_id="C1", name="hist", trigger_type="message",
        settings={}, description="d", script="await send_message('x')\n",
        created_by="U1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "C1",
            "trigger_type": "message",
            "trigger_context": {"author_id": "U7"},
        },
    )
    assert resp.json()["dispatched"] is True
    payload, _ = client.submitted[0]  # type: ignore[attr-defined]
    assert payload.trigger_context["guild_id"] == "G1"


async def test_dispatch_mod_action_not_under_member_raid_gate(
    client, db_session, monkeypatch
):
    from smarter_dev.web.models import AdminHandler

    # Simulate an exhausted member-events raid window; a mod_action fire must NOT
    # be keyed on it (mod actions are not a raid vector), so it still enqueues.
    # Capture the actual limiter instance the endpoint uses so its recorded hits
    # can be inspected (a fresh _KeyAwareLimiter().hits would always be empty).
    limiters: list[_KeyAwareLimiter] = []

    def make_limiter(redis):
        limiter = _KeyAwareLimiter()
        limiters.append(limiter)
        return limiter

    monkeypatch.setattr(handlers_module, "WindowedLimiter", make_limiter)
    db_session.add(AdminHandler(
        guild_id="G1", name="mod-log", trigger_type="mod_action", settings={},
        channel_ids=[], description="format", script="pass\n", created_by_admin="A1",
    ))
    await db_session.commit()

    resp = client.post(
        "/api/handlers/dispatch",
        json={
            "guild_id": "G1",
            "channel_id": "",
            "trigger_type": "mod_action",
            "trigger_context": {"trigger_type": "mod_action"},
        },
    )
    assert resp.json()["dispatched"] is True
    # The raid window key was never even consulted for a mod_action fire.
    assert limiters, "the endpoint constructed no limiter"
    all_hits = [key for limiter in limiters for key, _ in limiter.hits]
    assert not any("memberevt" in key for key in all_hits)


async def test_active_channels_lists_mod_action_as_guild_trigger(client, db_session):
    from smarter_dev.web.models import AdminHandler

    db_session.add(AdminHandler(
        guild_id="G1", name="mod-log", trigger_type="mod_action", settings={},
        channel_ids=["SOMECHAN"], description="format", script="pass\n",
        created_by_admin="A1",
    ))
    await db_session.commit()

    body = client.get("/api/handlers/active-channels").json()
    # A mod_action fire is guild-wide (no home channel), so it surfaces as a
    # (guild_id, trigger) guild-trigger even though the handler is scoped.
    assert ["G1", "mod_action"] in body["guild_triggers"]
    assert all(trigger != "mod_action" for _, trigger in body["channels"])
