"""Admin controller tests for the privacy purge pages.

Handlers are invoked through ``.fn`` like the sibling admin tests, with the
admin context, flash helpers and job submission patched. Synthetic user only.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import fakeredis.aioredis
import httpx
import pytest
from litestar.response import Redirect
from litestar.response import Template
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from sqlalchemy import select

from smarter_dev.shared.privacy_purge import enforcing_key
from smarter_dev.web.bot_admin.privacy_purge import PrivacyPurgeAdminController
from smarter_dev.web.chat_bot_purge import open_purge_request
from smarter_dev.web.discord_admin_client import DiscordAdminClient
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotPurgeRequest

_MODULE = "smarter_dev.web.bot_admin.privacy_purge"
_KAI_ID = "111111111111111111"
_GUILD = "123456789012345678"

_HANDLERS = [
    PrivacyPurgeAdminController.list_requests,
    PrivacyPurgeAdminController.lookup,
    PrivacyPurgeAdminController.create,
    PrivacyPurgeAdminController.view,
    PrivacyPurgeAdminController.rerun,
    PrivacyPurgeAdminController.check,
    PrivacyPurgeAdminController.close,
]


class _Request:
    def __init__(self, form: dict | None = None):
        self._form = form or {}

    async def form(self):
        return self._form


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


async def _runtimes_ready(redis, *, consumers: bool = True) -> None:
    for component in ("bot", "worker"):
        await redis.set(f"{enforcing_key(component)}:host-1", 1, ex=180)
        if consumers:
            await redis.set(f"privacy:v1:consumer:{component}:host-1", "1", ex=180)


@pytest.fixture
def patched(redis):
    flashes: list[str] = []
    submit_run = AsyncMock()
    with (
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_MODULE}.get_flash_messages", return_value=[]),
        patch(f"{_MODULE}.flash_error", side_effect=lambda _r, m: flashes.append(m)),
        patch(f"{_MODULE}.flash_success", side_effect=lambda _r, m: flashes.append(m)),
        patch(f"{_MODULE}.get_redis_client", return_value=redis),
        patch(f"{_MODULE}.submit_run", new=submit_run),
        patch(f"{_MODULE}.submit_check", new=AsyncMock()),
    ):
        yield SimpleNamespace(flashes=flashes, submit_run=submit_run)


def _no_id_in(response) -> None:
    if isinstance(response, Redirect):
        assert _KAI_ID not in response.url


# -- the administrator permission --------------------------------------------------


@pytest.mark.parametrize("handler", _HANDLERS, ids=lambda h: h.handler_name)
def test_every_route_requires_the_administrator_permission(handler):
    assert auth_guard in handler.guards
    assert any(
        isinstance(g, Permission) and g.permission == "administrator" for g in handler.guards
    )


async def test_a_non_admin_is_refused():
    guard = Permission("administrator")
    assert await guard.check(SimpleNamespace(permissions={"view-drafts"}, roles=set())) is False
    assert await guard.check(SimpleNamespace(permissions={"administrator"}, roles=set())) is True


# -- the ID never goes in a URL ----------------------------------------------------


def test_the_name_lookup_is_a_post_and_the_list_takes_no_user_id():
    assert "POST" in PrivacyPurgeAdminController.lookup.http_methods
    assert "user_id" not in PrivacyPurgeAdminController.list_requests.fn.__code__.co_varnames


async def test_the_lookup_renders_the_names_without_a_redirect(db_session, patched, redis):
    client = SimpleNamespace(get_user_names=AsyncMock(return_value=["kai"]))
    with (
        patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)),
        patch(f"{_MODULE}.get_admin_discord_client", return_value=client),
    ):
        response = await PrivacyPurgeAdminController.lookup.fn(
            None, request=_Request({"user_id": _KAI_ID}), db_session=db_session
        )
    assert isinstance(response, Template)
    assert response.context["lookup_id"] == _KAI_ID
    assert response.context["names"] == ["kai"]


# -- CSRF --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["lookup", "create"])
async def test_a_bad_csrf_token_is_refused(db_session, patched, redis, name):
    await _runtimes_ready(redis)
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=False)):
        response = await getattr(PrivacyPurgeAdminController, name).fn(
            None, request=_Request({"user_id": _KAI_ID, "names": "kai"}), db_session=db_session
        )
    assert isinstance(response, Redirect)
    _no_id_in(response)
    assert not (await db_session.scalars(select(ChatBotPurgeRequest))).all()
    assert not (await db_session.scalars(select(ChatBotBlockedUser))).all()
    patched.submit_run.assert_not_awaited()


@pytest.mark.parametrize("name", ["rerun", "close", "check"])
async def test_a_bad_csrf_token_is_refused_on_request_actions(db_session, patched, redis, name):
    await _runtimes_ready(redis)
    purge = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=["kai"], requested_by=None
    )
    await db_session.commit()
    run_id = purge.run_id
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=False)):
        response = await getattr(PrivacyPurgeAdminController, name).fn(
            None, request=_Request(), db_session=db_session, request_id=purge.id
        )
    assert isinstance(response, Redirect)
    await db_session.refresh(purge)
    assert purge.run_id == run_id and purge.discord_user_id == _KAI_ID
    patched.submit_run.assert_not_awaited()


# -- refusing to start --------------------------------------------------------------


@pytest.mark.parametrize("consumers", [False, None], ids=["no-consumer", "not-enforcing"])
async def test_a_start_is_refused_unless_both_runtimes_enforce_and_consume(
    db_session, patched, redis, consumers
):
    if consumers is False:
        await _runtimes_ready(redis, consumers=False)
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)):
        response = await PrivacyPurgeAdminController.create.fn(
            None, request=_Request({"user_id": _KAI_ID, "names": "kai"}), db_session=db_session
        )
    # Rendered, not redirected: no URL carries the ID.
    assert isinstance(response, Template)
    assert response.context["lookup_id"] == _KAI_ID
    expected = "No live purge consumer" if consumers is False else "Not enforcing"
    assert any(expected in message for message in patched.flashes)
    assert not (await db_session.scalars(select(ChatBotPurgeRequest))).all()
    patched.submit_run.assert_not_awaited()


async def test_a_rerun_is_refused_without_a_live_consumer(db_session, patched, redis):
    await _runtimes_ready(redis, consumers=False)
    purge = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=["kai"], requested_by=None
    )
    await db_session.commit()
    run_id = purge.run_id
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)):
        response = await PrivacyPurgeAdminController.rerun.fn(
            None, request=_Request(), db_session=db_session, request_id=purge.id
        )
    _no_id_in(response)
    await db_session.refresh(purge)
    assert purge.run_id == run_id
    patched.submit_run.assert_not_awaited()


async def test_a_start_with_both_runtimes_ready_opens_and_submits(db_session, patched, redis):
    await _runtimes_ready(redis)
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)):
        response = await PrivacyPurgeAdminController.create.fn(
            None, request=_Request({"user_id": _KAI_ID, "names": "kai"}), db_session=db_session
        )
    assert isinstance(response, Redirect)
    _no_id_in(response)
    patched.submit_run.assert_awaited_once()


async def test_the_list_page_shows_processes_and_consumers(db_session, patched, redis):
    await _runtimes_ready(redis)
    response = await PrivacyPurgeAdminController.list_requests.fn(
        None, request=_Request(), db_session=db_session
    )
    assert response.context["runtimes"]["bot"] == {"processes": 1, "revision": 1, "consumers": 1}


# -- the Discord lookup never logs the ID -------------------------------------------


async def test_the_name_lookup_never_logs_the_user_id(caplog):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/users/{_KAI_ID}"):
            return httpx.Response(200, json={"username": "kai", "global_name": "Kai"})
        if "/members/" in request.url.path:
            return httpx.Response(404, json={"message": f"Unknown member {_KAI_ID}"})
        return httpx.Response(200, json={})

    client = DiscordAdminClient(bot_token="t", transport=httpx.MockTransport(respond))
    with caplog.at_level(logging.DEBUG):
        names = await client.get_user_names(_KAI_ID, [_GUILD])
        # The filter is narrow: other Discord requests are still logged.
        await client._request("GET", f"/guilds/{_GUILD}")

    assert names == ["kai", "Kai"]
    assert all(_KAI_ID not in record.getMessage() for record in caplog.records)
    assert any(_GUILD in record.getMessage() for record in caplog.records if record.name == "httpx")


async def test_a_transport_failure_in_the_lookup_is_swallowed_without_logging(caplog):
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = DiscordAdminClient(bot_token="t", transport=httpx.MockTransport(fail))
    with caplog.at_level(logging.DEBUG):
        assert await client.get_user_names(_KAI_ID, [_GUILD]) == []
    assert all(_KAI_ID not in record.getMessage() for record in caplog.records)


# -- database errors never log their parameters (review item 9) ---------------------


async def test_a_database_error_on_start_logs_the_type_only(db_session, patched, redis, caplog):
    from sqlalchemy.exc import IntegrityError

    await _runtimes_ready(redis)
    error = IntegrityError("INSERT INTO chat_bot_blocked_users", {"id": _KAI_ID}, Exception(_KAI_ID))
    with (
        patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)),
        patch(f"{_MODULE}.open_purge_request", new=AsyncMock(side_effect=error)),
        caplog.at_level(logging.DEBUG),
    ):
        response = await PrivacyPurgeAdminController.create.fn(
            None, request=_Request({"user_id": _KAI_ID, "names": "kai"}), db_session=db_session
        )
    assert isinstance(response, Template)
    assert all(_KAI_ID not in record.getMessage() for record in caplog.records)
    assert any("IntegrityError" in record.getMessage() for record in caplog.records)
    patched.submit_run.assert_not_awaited()


def test_the_new_id_columns_fit_a_22_digit_snowflake():
    assert ChatBotBlockedUser.__table__.c.discord_user_id.type.length == 22
    assert ChatBotPurgeRequest.__table__.c.discord_user_id.type.length == 22


async def test_a_failed_purge_job_raises_without_the_error_text():
    import uuid

    from sqlalchemy.exc import IntegrityError

    from smarter_dev.web import chat_bot_purge_jobs

    error = IntegrityError("UPDATE chat_bot_purge_requests", {"id": _KAI_ID}, Exception(_KAI_ID))
    payload = chat_bot_purge_jobs.ChatBotPurgePayload(request_id=uuid.uuid4(), run_id=uuid.uuid4())
    with (
        patch.object(chat_bot_purge_jobs, "run_purge", new=AsyncMock(side_effect=error)),
        patch.object(chat_bot_purge_jobs, "get_redis_client"),
        pytest.raises(chat_bot_purge_jobs.PurgeJobFailed) as raised,
    ):
        await chat_bot_purge_jobs.run_chat_bot_purge(payload)
    assert str(raised.value) == "IntegrityError"
    assert raised.value.__context__ is None and raised.value.__cause__ is None
