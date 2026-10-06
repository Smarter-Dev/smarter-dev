"""Deleting a site account rewrites the creator values that name it.

Each who-did-it column is checked against the test database: values that are
exactly one of the account's identifiers become ``DELETED``; the app's own
markers, other people's values and values that merely contain an identifier
stay.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import Text
from sqlalchemy import select
from sqlalchemy.types import JSON

from smarter_dev.web.account_identifiers import CREATOR_COLUMNS
from smarter_dev.web.account_identifiers import forget_admin_identifiers
from smarter_dev.web.chat import jobs as chat_jobs
from smarter_dev.web.models import AccountDeletionRequest
from smarter_dev.web.models import Campaign
from smarter_dev.web.models import ChatBotPurgeRequest
from smarter_dev.web.models import ScheduledMessage
from tests.web.account_signups_test import _MY_ID
from tests.web.account_signups_test import _THEIR_ID
from tests.web.account_signups_test import _login
from tests.web.user_content_test import _Storage
from tests.web.user_content_test import _user


def _row(model, **values):
    """A row of ``model`` with every required column filled with a stand-in."""
    if "trigger_type" in model.__table__.columns:
        values.setdefault("trigger_type", "message")
    for column in model.__table__.columns:
        if column.key in values or column.primary_key or column.nullable:
            continue
        if column.default is not None or column.server_default is not None:
            continue
        kind = column.type
        if isinstance(kind, DateTime):
            values[column.key] = datetime.now(UTC)
        elif isinstance(kind, Boolean):
            values[column.key] = False
        elif isinstance(kind, Integer):
            values[column.key] = 1
        elif isinstance(kind, JSON):
            values[column.key] = {}
        elif isinstance(kind, Text):
            values[column.key] = "text"
        elif getattr(kind, "python_type", None) is UUID or "UUID" in str(kind):
            values[column.key] = uuid4()
        else:
            length = getattr(kind, "length", None) or 20
            values[column.key] = uuid4().hex[: min(length, 12)]
    return model(**values)


async def _seed(db_session, column, values: list[str]) -> dict:
    """One row per value in ``column``'s table; returns row id → value."""
    model = column.class_
    extra = {}
    if model is ScheduledMessage:
        campaign = _row(Campaign, created_by="admin")
        db_session.add(campaign)
        await db_session.flush()
        extra["campaign_id"] = campaign.id
    rows = {}
    for value in values:
        row = _row(model, **extra, **{column.key: value})
        db_session.add(row)
        await db_session.flush()
        rows[row.id] = value
    await db_session.commit()
    return rows


async def _values(db_session, column) -> dict:
    db_session.expire_all()
    model = column.class_
    return dict((await db_session.execute(select(model.id, column))).all())


async def _admin(db_session, *, name="Ada Admin"):
    user = await _user(db_session)
    user.name = name
    user.email = "Ada@Example.test"
    await db_session.commit()
    await _login(
        db_session, user, "discord", subject=_MY_ID, email="ada.discord@example.test"
    )
    await _login(
        db_session, user, "github", email="ada.unverified@example.test", verified=False
    )
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column",
    CREATOR_COLUMNS,
    ids=[f"{c.class_.__tablename__}.{c.key}" for c in CREATOR_COLUMNS],
)
async def test_each_creator_column_loses_exactly_the_accounts_identifiers(
    db_session, column
):
    user = await _admin(db_session)
    width = column.type.length
    named = [
        "ada@example.TEST",  # the account email, any case
        "ADA ADMIN",  # the account name
        _MY_ID,  # the linked Discord login
        "ada.discord@example.test",  # a confirmed login email
    ]
    kept = [
        "ada.unverified@example.test",  # a login email nobody confirmed
        "admin",  # the app's own markers
        "chatbot",
        "extension",
        "deleted",
        "other@example.test",  # someone else
        _THEIR_ID,
        "Ada Admin 2",  # contains the name, is not it
    ]
    rows = await _seed(db_session, column, [v for v in named + kept if len(v) <= width])

    await forget_admin_identifiers(db_session, user.id)
    await db_session.commit()

    after = await _values(db_session, column)
    for row_id, before in rows.items():
        expected = "DELETED" if before in named else before
        assert after[row_id] == expected, before


@pytest.mark.asyncio
async def test_an_account_named_like_a_marker_leaves_the_marker(db_session):
    user = await _admin(db_session, name="Admin")
    column = CREATOR_COLUMNS[0]
    rows = await _seed(db_session, column, ["admin", "Admin"])

    await forget_admin_identifiers(db_session, user.id)
    await db_session.commit()

    assert set((await _values(db_session, column)).values()) == {"admin", "Admin"}
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_the_accounts_purge_requests_lose_who_asked(db_session):
    user = await _admin(db_session)
    other = uuid4()
    mine = _row(ChatBotPurgeRequest, requested_by=str(user.id))
    theirs = _row(ChatBotPurgeRequest, requested_by=str(other))
    db_session.add_all([mine, theirs])
    await db_session.commit()
    mine_id, theirs_id = mine.id, theirs.id

    await forget_admin_identifiers(db_session, user.id)
    await db_session.commit()

    assert await _values(db_session, ChatBotPurgeRequest.requested_by) == {
        mine_id: None,
        theirs_id: str(other),
    }


@pytest.mark.asyncio
async def test_deleting_the_account_rewrites_what_names_it(db_session, monkeypatch):
    user = await _admin(db_session)
    install = CREATOR_COLUMNS[0]
    rows = await _seed(db_session, install, ["ada@example.test", "admin"])
    request = _row(ChatBotPurgeRequest, requested_by=str(user.id))
    db_session.add(request)
    deletion = AccountDeletionRequest(user_id=user.id, status="pending")
    db_session.add(deletion)
    await db_session.commit()
    request_id = request.id

    @asynccontextmanager
    async def session_context():
        yield db_session

    class _Manager:
        def __init__(self, _settings):
            pass

        async def get(self, _name):
            return _Storage()

        async def close(self):
            pass

    monkeypatch.setattr(chat_jobs, "get_db_session_context", session_context)
    monkeypatch.setattr("skrift.storage.StorageManager", _Manager)
    monkeypatch.setattr(
        "skrift.config.get_settings", lambda: SimpleNamespace(storage=None)
    )

    assert await chat_jobs.delete_chat_account(
        chat_jobs.ChatAccountDeletionPayload(request_id=str(deletion.id))
    ) == {"status": "deleted"}

    assert sorted((await _values(db_session, install)).values()) == [
        "DELETED",
        "admin",
    ]
    assert len(rows) == 2
    assert (await _values(db_session, ChatBotPurgeRequest.requested_by)) == {
        request_id: None
    }


def test_every_creator_column_named_for_deletion_is_rewritten():
    assert {(c.class_.__tablename__, c.key) for c in CREATOR_COLUMNS} == {
        ("extension_installs", "installed_by"),
        ("channel_handlers", "created_by"),
        ("forum_agents", "created_by"),
        ("campaigns", "created_by"),
        ("scheduled_messages", "created_by"),
        ("squad_sale_events", "created_by"),
        ("repeating_messages", "created_by"),
        ("admin_handlers", "created_by_admin"),
    }
