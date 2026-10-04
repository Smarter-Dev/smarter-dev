"""Admin controller tests for the privacy purge pages.

Handlers are invoked through ``.fn`` like the sibling admin tests, with the
admin context, flash helpers and job submission patched. Synthetic user only.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
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
    PrivacyPurgeAdminController.remove_name,
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
    mark_failed = AsyncMock(return_value=True)
    with (
        patch.object(chat_bot_purge_jobs, "run_purge", new=AsyncMock(side_effect=error)),
        patch.object(chat_bot_purge_jobs, "get_redis_client"),
        patch.object(chat_bot_purge_jobs, "mark_failed", new=mark_failed),
        pytest.raises(chat_bot_purge_jobs.PurgeJobFailed) as raised,
    ):
        await chat_bot_purge_jobs.run_chat_bot_purge(payload)
    assert str(raised.value) == "IntegrityError"
    # The request is left "failed" (L5), with the type only.
    assert mark_failed.await_args.args[1:] == (payload.request_id, payload.run_id, "IntegrityError")
    assert raised.value.__context__ is None and raised.value.__cause__ is None


# == second review round ===========================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", ""),
        ("post", "/lookup"),
        ("post", ""),
        ("get", "/{rid}"),
        ("post", "/{rid}/rerun"),
        ("post", "/{rid}/check"),
        ("post", "/{rid}/close"),
        ("post", "/{rid}/names/remove"),
    ],
)
def test_a_signed_in_non_admin_is_refused_on_every_route(method, path):
    """HTTP level: a real session for a user without ``administrator``."""
    import contextlib
    import uuid

    from litestar.middleware.session.server_side import ServerSideSessionConfig
    from litestar.testing import create_test_client
    from skrift.auth.services import UserPermissions
    from skrift.auth.session_keys import SESSION_USER_ID

    @contextlib.asynccontextmanager
    async def null_session():
        yield SimpleNamespace()

    async def permissions(session, user_id):
        return UserPermissions(user_id=str(user_id), permissions={"view-drafts"})

    handler_called = AsyncMock()
    session_config = ServerSideSessionConfig()
    with (
        patch("skrift.auth.services.get_user_permissions", side_effect=permissions),
        patch(f"{_MODULE}.verify_csrf", new=handler_called),
        patch(f"{_MODULE}.get_admin_context", new=handler_called),
        create_test_client(
            route_handlers=[PrivacyPurgeAdminController],
            middleware=[session_config.middleware],
            session_config=session_config,
        ) as client,
    ):
        client.app.state.session_maker_class = null_session
        client.set_session_data({SESSION_USER_ID: str(uuid.uuid4())})
        url = "/admin/bot/privacy-purges" + path.format(rid=uuid.uuid4())
        kwargs = {"data": {"user_id": _KAI_ID}} if method == "post" else {}
        response = getattr(client, method)(url, **kwargs)
    assert response.status_code == 401
    handler_called.assert_not_awaited()


async def test_the_stored_names_lookup_failure_logs_the_type_only(db_session, caplog):
    from smarter_dev.web.bot_admin.privacy_purge import known_names

    client = SimpleNamespace(get_user_names=AsyncMock(return_value=["kai"]))
    with (
        patch(f"{_MODULE}.get_admin_discord_client", return_value=client),
        patch(f"{_MODULE}.affected_guild_ids", new=AsyncMock(return_value=[])),
        patch.object(
            db_session, "execute", new=AsyncMock(side_effect=RuntimeError(f"WHERE id = {_KAI_ID}"))
        ),
        patch.object(db_session, "rollback", new=AsyncMock()),
        caplog.at_level(logging.DEBUG),
    ):
        assert await known_names(db_session, _KAI_ID) == ["kai"]
    assert all(_KAI_ID not in record.getMessage() for record in caplog.records)
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)


def test_a_component_behind_the_block_list_is_shown_as_such():
    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)
    html = env.get_template("admin/bot/privacy_purges/_runtimes.html").render(
        runtimes={
            "bot": {"processes": 2, "revision": 4, "consumers": 1},
            "worker": {"processes": 1, "revision": 5, "consumers": 1},
        },
        list_revision=5,
    )
    assert "behind the block list (holding revision 4 of 5)" in html
    assert "waits for that process" in html
    assert html.count("behind the block list") == 1


async def test_close_is_refused_while_a_guild_is_tombstoned(db_session, patched, redis):
    from smarter_dev.shared.privacy_purge import history_tombstone_key

    purge = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=["kai"], requested_by=None
    )
    purge.steps["guild_ids"] = [_GUILD]
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(purge, "steps")
    await db_session.commit()
    await redis.set(history_tombstone_key(_GUILD), "1")
    purge_id = purge.id
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)):
        await PrivacyPurgeAdminController.close.fn(
            None, request=_Request(), db_session=db_session, request_id=purge_id
        )
    stored = await db_session.get(ChatBotPurgeRequest, purge_id, populate_existing=True)
    assert stored.discord_user_id == _KAI_ID and stored.status != "closed"
    assert any("tombstoned" in m and _GUILD in m for m in patched.flashes)


async def test_run_the_check_again_submits_the_scan_only_check(db_session, patched, redis):
    import uuid

    from smarter_dev.web import bot_admin

    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)):
        await PrivacyPurgeAdminController.check.fn(
            None, request=_Request(), db_session=db_session, request_id=uuid.uuid4()
        )
    submitted = bot_admin.privacy_purge.submit_check
    assert submitted.await_args.kwargs == {"scan_only": True}


def test_logfire_does_not_instrument_httpx_or_sqlalchemy_here():
    """Tripwire: skrift instruments httpx and SQLAlchemy only with
    ``logfire.enabled`` (off: app.yaml has no logfire section) and only if the
    OpenTelemetry instrumentors are installed (they are not locked). Turning
    either on puts Discord URLs (the user ID) into spans: scrub the purge's
    lookups first."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    for name in ("app.yaml", "app.development.yaml"):
        config = yaml.safe_load((root / name).read_text()) or {}
        assert not (config.get("logfire") or {}).get("enabled")
    lock = (root / "uv.lock").read_text()
    assert 'name = "opentelemetry-instrumentation-httpx"' not in lock
    assert 'name = "opentelemetry-instrumentation-sqlalchemy"' not in lock


# == third round: drop an unchecked name; ack timeout ===============================


async def _request_with_names(db_session, names, status="needs_review"):
    purge = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=names, requested_by=None
    )
    purge.status = status
    await db_session.commit()
    return purge.id


@pytest.mark.parametrize(
    ("name", "status", "lease", "removed"),
    [
        ("k", "needs_review", None, True),
        ("kai", "needs_review", None, False),  # it was searched: never removable
        ("k", "purging", None, False),
        ("k", "finishing", None, False),
        ("k", "awaiting_acks", "2099-01-01T00:00:00+00:00", False),  # a run holds it
        ("k", "awaiting_acks", "2000-01-01T00:00:00+00:00", True),  # a dead run's lease
    ],
    ids=["unchecked", "searched", "purging", "finishing", "lease-held", "lease-expired"],
)
async def test_only_an_unchecked_name_is_removed_and_only_while_no_run_holds_it(
    db_session, patched, name, status, lease, removed
):
    from sqlalchemy.orm.attributes import flag_modified

    request_id = await _request_with_names(db_session, ["kai", "k"], status)
    if lease:
        purge = await db_session.get(ChatBotPurgeRequest, request_id)
        purge.steps["lease"] = {"token": "other", "until": lease}
        flag_modified(purge, "steps")
        await db_session.commit()
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)):
        response = await PrivacyPurgeAdminController.remove_name.fn(
            None, request=_Request({"name": name}), db_session=db_session, request_id=request_id
        )
    _no_id_in(response)
    purge = await db_session.get(ChatBotPurgeRequest, request_id, populate_existing=True)
    expected = [n for n in ["kai", "k"] if not (removed and n == name)]
    assert purge.names == expected


async def test_name_removal_needs_csrf(db_session, patched):
    request_id = await _request_with_names(db_session, ["kai", "k"])
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=False)):
        await PrivacyPurgeAdminController.remove_name.fn(
            None, request=_Request({"name": "k"}), db_session=db_session, request_id=request_id
        )
    purge = await db_session.get(ChatBotPurgeRequest, request_id, populate_existing=True)
    assert purge.names == ["kai", "k"]


async def test_a_removed_name_is_not_merged_back_unless_entered_again(db_session, patched):
    from smarter_dev.web.chat_bot_purge import remove_unchecked_name

    request_id = await _request_with_names(db_session, ["kai", "k"])
    assert await remove_unchecked_name(db_session, request_id, "k", now=datetime.now(UTC)) is None
    await db_session.commit()
    again = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=["Kai the Rustacean"], requested_by=None
    )
    assert again.names == ["kai", "Kai the Rustacean"]
    again = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=["k"], requested_by=None
    )
    assert again.names == ["kai", "Kai the Rustacean", "k"]


def test_the_page_offers_removal_beside_each_unchecked_name():
    import jinja2
    from markupsafe import Markup

    env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)
    source = env.loader.get_source(env, "admin/bot/privacy_purges/view.html")[0]
    start = source.index("{% if unchecked_names")
    block = source[start : source.index("{% endif %}", start) + len("{% endif %}")]
    html = env.from_string(block).render(
        unchecked_names=("k",),
        purge=SimpleNamespace(id="rid", status="needs_review"),
        csrf_field=lambda: Markup("<csrf>"),
    )
    assert "Remove (never searched)" in html
    assert 'action="/admin/bot/privacy-purges/rid/names/remove"' in html
    assert '<input type="hidden" name="name" value="k">' in html and "<csrf>" in html
