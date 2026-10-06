"""A member sees and deletes their own campaign sign-ups, and nobody else's.

A sign-up row has no link to an account; it is the member's when its email is
one a linked login confirmed, or its Discord ID is their linked Discord login.
The routes and the account deletion job run against the test database.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from litestar import Response
from litestar.exceptions import NotAuthorizedException
from skrift.db.models.oauth_account import OAuthAccount
from sqlalchemy import select

from smarter_dev.web import account_controller
from smarter_dev.web.account_controller import AccountController
from smarter_dev.web.account_signups import delete_account_signups
from smarter_dev.web.account_signups import list_account_signups
from smarter_dev.web.chat import jobs as chat_jobs
from smarter_dev.web.exception_handlers import http_exception_handler
from smarter_dev.web.models import AccountDeletionRequest
from smarter_dev.web.models import CampaignSignup
from tests.web.user_content_test import _request
from tests.web.user_content_test import _Storage
from tests.web.user_content_test import _user

REPO = Path(__file__).resolve().parents[2]
# Discord user IDs, made up.
_MY_ID = "123456789012345678"
_MY_OTHER_ID = "111111111111111111"
_THEIR_ID = "222222222222222222"
_STRANGER_ID = "333333333333333333"


async def _login(
    db_session, user, provider, *, subject=None, email=None, verified=True
) -> None:
    db_session.add(
        OAuthAccount(
            provider=provider,
            provider_account_id=subject or uuid4().hex,
            provider_email=email,
            provider_email_verified=verified,
            user_id=user.id,
        )
    )
    await db_session.commit()


async def _signup(db_session, *, email=None, discord_id=None, slug="sudo-launch"):
    row = CampaignSignup(campaign_slug=slug, email=email, discord_id=discord_id)
    db_session.add(row)
    await db_session.commit()
    return row.id


async def _left(db_session) -> set:
    db_session.expire_all()
    return set((await db_session.scalars(select(CampaignSignup.id))).all())


# ── Which rows are the member's ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_confirmed_login_email_finds_the_sign_up_in_any_case(db_session):
    user = await _user(db_session)
    await _login(db_session, user, "google", email="Member@Example.test")
    mine = await _signup(db_session, email="member@example.TEST")

    [signup] = await list_account_signups(db_session, user.id)

    assert (signup.id, signup.campaign, signup.email, signup.discord) == (
        mine,
        "sudo membership waitlist",
        "member@example.TEST",
        False,
    )


@pytest.mark.asyncio
async def test_an_unconfirmed_login_email_finds_nothing(db_session):
    user = await _user(db_session)
    # The account's own email, and a login that holds it unverified: neither
    # proves the member controls the address.
    await _login(db_session, user, "github", email=user.email, verified=False)
    await _signup(db_session, email=user.email)

    assert await list_account_signups(db_session, user.id) == []
    assert await delete_account_signups(db_session, user.id) == 0
    assert len(await _left(db_session)) == 1


@pytest.mark.asyncio
async def test_the_discord_login_finds_the_sign_up_by_discord_id(db_session):
    user = await _user(db_session)
    await _login(db_session, user, "discord", subject=_MY_ID)
    mine = await _signup(db_session, discord_id=_MY_ID)

    [signup] = await list_account_signups(db_session, user.id)

    assert (signup.id, signup.email, signup.discord) == (mine, None, True)


@pytest.mark.asyncio
async def test_another_providers_account_id_is_not_a_discord_id(db_session):
    user = await _user(db_session)
    await _login(db_session, user, "github", subject=_MY_ID)
    await _signup(db_session, discord_id=_MY_ID)

    assert await list_account_signups(db_session, user.id) == []


@pytest.mark.asyncio
async def test_a_member_sees_and_deletes_none_of_another_members_rows(db_session):
    me = await _user(db_session)
    other = await _user(db_session)
    await _login(
        db_session, me, "discord", subject=_MY_OTHER_ID, email="me@example.test"
    )
    await _login(
        db_session,
        other,
        "discord",
        subject=_THEIR_ID,
        email="other@example.test",
    )
    mine = await _signup(db_session, email="me@example.test")
    theirs_by_email = await _signup(db_session, email="other@example.test")
    theirs_by_discord = await _signup(db_session, discord_id=_THEIR_ID)
    stranger = await _signup(db_session, email="nobody@example.test")

    assert [s.id for s in await list_account_signups(db_session, me.id)] == [mine]
    assert await delete_account_signups(db_session, me.id, theirs_by_email) == 0
    assert await delete_account_signups(db_session, me.id, theirs_by_discord) == 0
    assert await delete_account_signups(db_session, me.id) == 1
    await db_session.commit()

    assert await _left(db_session) == {theirs_by_email, theirs_by_discord, stranger}


@pytest.mark.asyncio
async def test_a_row_found_by_discord_does_not_show_an_email_that_is_not_theirs(
    db_session,
):
    user = await _user(db_session)
    await _login(db_session, user, "discord", subject=_MY_ID)
    await _signup(db_session, email="someone@example.test", discord_id=_MY_ID)

    [signup] = await list_account_signups(db_session, user.id)

    assert (signup.email, signup.discord) == (None, True)


# ── The account page routes ─────────────────────────────────────────


def _routes(monkeypatch, user):
    flashed = []

    async def csrf_ok(_request):
        return True

    monkeypatch.setattr(account_controller, "verify_csrf", csrf_ok)
    monkeypatch.setattr(
        account_controller, "flash_success", lambda _r, text: flashed.append(text)
    )
    monkeypatch.setattr(
        account_controller, "flash_error", lambda _r, text: flashed.append(text)
    )
    request = _request(user.id)
    request.session["user_id"] = user.id
    return object.__new__(AccountController), request, flashed


@pytest.mark.asyncio
async def test_deleting_one_sign_up_leaves_the_others(db_session, monkeypatch):
    user = await _user(db_session)
    await _login(
        db_session,
        user,
        "discord",
        subject=_MY_ID,
        email="me@example.test",
    )
    first = await _signup(db_session, email="me@example.test")
    second = await _signup(db_session, discord_id=_MY_ID, slug="other")
    controller, request, flashed = _routes(monkeypatch, user)

    response = await AccountController.delete_signup.fn(
        controller, request, db_session, first
    )

    assert response.url == "/account/security"
    assert flashed == ["Sign-up deleted."]
    assert await _left(db_session) == {second}


@pytest.mark.asyncio
async def test_deleting_someone_elses_sign_up_by_id_is_refused(db_session, monkeypatch):
    user = await _user(db_session)
    other = await _user(db_session)
    await _login(db_session, other, "discord", subject=_THEIR_ID)
    theirs = await _signup(db_session, discord_id=_THEIR_ID)
    controller, request, flashed = _routes(monkeypatch, user)

    await AccountController.delete_signup.fn(controller, request, db_session, theirs)

    assert flashed == ["That sign-up is not one of yours."]
    assert await _left(db_session) == {theirs}


@pytest.mark.asyncio
async def test_delete_all_takes_every_sign_up_of_the_member(db_session, monkeypatch):
    user = await _user(db_session)
    other = await _user(db_session)
    await _login(
        db_session,
        user,
        "discord",
        subject=_MY_ID,
        email="me@example.test",
    )
    await _login(db_session, other, "google", email="other@example.test")
    await _signup(db_session, email="me@example.test")
    await _signup(db_session, discord_id=_MY_ID, slug="other")
    theirs = await _signup(db_session, email="other@example.test")
    controller, request, flashed = _routes(monkeypatch, user)

    response = await AccountController.delete_your_data.fn(
        controller, request, db_session, "signups"
    )

    assert response.url == "/account/security"
    assert flashed == ["Deleted 2 sign-ups."]
    assert await _left(db_session) == {theirs}


# ── Account deletion ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_the_account_takes_its_sign_ups(db_session, monkeypatch):
    user = await _user(db_session)
    other = await _user(db_session)
    await _login(
        db_session,
        user,
        "discord",
        subject=_MY_ID,
        email="me@example.test",
    )
    await _login(db_session, other, "google", email="other@example.test")
    await _signup(db_session, email="me@example.test")
    await _signup(db_session, discord_id=_MY_ID, slug="other")
    theirs = await _signup(db_session, email="other@example.test")
    stranger = await _signup(db_session, discord_id=_STRANGER_ID)
    deletion = AccountDeletionRequest(user_id=user.id, status="pending")
    db_session.add(deletion)
    await db_session.commit()

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

    assert await _left(db_session) == {theirs, stranger}


# ── Where the member finds them ──────────────────────────────────────


def test_the_security_page_lists_sign_ups_under_your_data():
    from tests.web.test_sign_in_methods import _render_security_page

    first, second = uuid4(), uuid4()
    html = _render_security_page(
        sign_in_methods=["discord"],
        passkey_sign_in=False,
        passkey_available=False,
        linked_accounts=[],
        passkeys=[],
        your_data={},
        signups=[
            SimpleNamespace(
                id=first,
                campaign="sudo membership waitlist",
                email="me@example.test",
                discord=False,
                email_confirmed=False,
            ),
            SimpleNamespace(
                id=second,
                campaign="other",
                email=None,
                discord=True,
                email_confirmed=False,
            ),
        ],
    )

    assert 'id="your-data"' in html
    assert "2 sign-ups" in html
    assert 'action="/account/data/signups/delete"' in html
    assert "Delete all 2 sign-ups?" in html
    assert f'action="/account/data/signups/{first}/delete"' in html
    assert f'action="/account/data/signups/{second}/delete"' in html
    assert "Sudo membership waitlist" in html
    assert "<code>me@example.test</code> • not confirmed" in html
    assert "Your Discord account" in html


def test_no_sign_ups_offers_no_delete():
    from tests.web.test_sign_in_methods import _render_security_page

    html = _render_security_page(
        sign_in_methods=["discord"],
        passkey_sign_in=False,
        passkey_available=False,
        linked_accounts=[],
        passkeys=[],
        your_data={},
        signups=[],
    )

    assert "0 sign-ups" in html
    assert "/account/data/signups/" not in html


# ── Signing in on the way to the account page ────────────────────────


def _visit(path, *, method="GET", accept="text/html", signed_in=False):
    return SimpleNamespace(
        method=method,
        headers={"accept": accept},
        scope={"session": {"user_id": "member-id"} if signed_in else {}},
        url=SimpleNamespace(path=path, query=""),
    )


def test_a_signed_out_visit_to_the_account_page_goes_through_login():
    response = http_exception_handler(
        _visit("/account/security"), NotAuthorizedException()
    )

    assert response.status_code == 303
    parsed = urlsplit(response.url)
    assert parsed.path == "/auth/login"
    assert parse_qs(parsed.query) == {"next": ["/account/security"]}


@pytest.mark.parametrize(
    "request_",
    [
        _visit("/account/security", accept="application/json"),
        _visit("/account/data/signups/delete", method="POST"),
        _visit("/accounts-elsewhere"),
    ],
    ids=["api caller", "form post", "not an account path"],
)
def test_other_unauthorised_requests_still_get_401(request_):
    fallback = Response(content="Unauthorized", status_code=401)

    with patch(
        "smarter_dev.web.exception_handlers.skrift_http_exception_handler",
        return_value=fallback,
    ):
        response = http_exception_handler(request_, NotAuthorizedException())

    assert response is fallback


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("hash_", "expected"),
    [
        ("#your-data", "/account/security#your-data"),
        ("", "/account/security"),
        ('#a"b', "/account/security"),
    ],
)
def test_the_login_page_carries_the_section_into_next(hash_, expected):
    """The browser keeps ``#your-data`` across the 303 to the login page; the
    provider round trip would drop it, so the login page puts it in ``next``.
    """
    template = (REPO / "themes/smarterdev/templates/auth/login.html").read_text()
    script = re.findall(r"<script[^>]*>(.*?)</script>", template, re.S)[-1]
    harness = f"""
    const link = {{href: "/auth/discord/login"}};
    globalThis.location = {{
        search: "?next=%2Faccount%2Fsecurity", hash: {json.dumps(hash_)},
    }};
    globalThis.document = {{querySelectorAll: () => [link]}};
    {script}
    process.stdout.write(link.href);
    """
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )

    href = urlsplit(result.stdout)
    assert href.path == "/auth/discord/login"
    assert parse_qs(href.query) == {"next": [expected]}
