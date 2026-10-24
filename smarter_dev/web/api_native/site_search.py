"""Site service facade over member web searches, for search inside site chat.

The site Worker decides per chat turn whether the web is needed, then starts a
search here on the member's behalf and reads its progress. Runs are the same
``WebSearchRun`` rows the dashboard uses: the same planner, Brave, Jev ranking
and grounded answer on the agent worker, the same limits and the same
deletion rules. The member is the synced site user named in the path; the
service key is the only authority, and a browser never reaches these routes.

A signed-out visitor using a member's search link gets an anonymous search
(``web_search.anonymous``): never saved, kept in Redis for 30 minutes, billed
to the link's owner, under the anonymous limits. The Worker names the visitor
by an opaque key it derives; Python never sees the visitor's address.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import Response
from litestar import delete
from litestar import get
from litestar import post
from litestar.exceptions import HTTPException
from litestar.exceptions.responses import create_exception_response
from litestar.status_codes import HTTP_201_CREATED
from litestar.status_codes import HTTP_204_NO_CONTENT
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from skrift.auth.guards import Permission
from skrift.db.models.user import User
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web import dashboard_controller as dashboard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import BotApiException
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.models import WebSearchForgottenKey
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.user_content import StillRunning
from smarter_dev.web.user_content import delete_search
from smarter_dev.web.web_search import anonymous
from smarter_dev.web.web_search.snapshot import MAX_REQUEST_CHARS
from smarter_dev.web.web_search.snapshot import structured as site_snapshot

SITE_SEARCH_GUARDS = [*SITE_API_GUARDS, Permission("site-search")]


class StartSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=1, max_length=2000)
    # One key per chat turn: a retried start returns the same run.
    key: str = Field(min_length=8, max_length=64)


class StartAnonymousSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=1, max_length=2000)
    # The Worker's key for the visitor, rotated daily. No dots or colons, so
    # it never shares a limit with an address from the /s/ pages.
    visitor: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")


def _account_id(user_id: str) -> UUID:
    try:
        uid = UUID(user_id)
        if str(uid) != user_id.lower():
            raise ValueError
    except ValueError:
        raise plain_error(400, "Invalid account ID") from None
    return uid


async def _member(session: AsyncSession, user_id: str, *, lock=False) -> User:
    query = select(User).where(
        User.id == _account_id(user_id), User.is_active.is_(True)
    )
    user = await session.scalar(query.with_for_update() if lock else query)
    if user is None:
        raise plain_error(404, "Account not found")
    return user


def _key(key: str) -> str:
    try:
        return dashboard._validate_submission_key(key)
    except HTTPException as exc:
        raise plain_error(422, _detail(exc)) from None


async def _run_for_key(session: AsyncSession, owner: UUID, key: str):
    return await session.scalar(
        select(WebSearchRun).where(
            WebSearchRun.owner_user_id == owner,
            WebSearchRun.submission_key == key,
        )
    )


async def _forgotten(session: AsyncSession, owner: UUID, key: str) -> bool:
    return await session.get(WebSearchForgottenKey, (owner, key)) is not None


async def _owned(session: AsyncSession, user: User, run_id: str) -> WebSearchRun:
    try:
        rid = UUID(run_id)
    except ValueError:
        raise plain_error(404, "Search not found") from None
    run = await session.get(WebSearchRun, rid)
    if run is None or run.owner_user_id != user.id:
        raise plain_error(404, "Search not found")
    return run


async def _owner_of(session: AsyncSession, owner_id: str) -> UUID:
    """The member whose search link the site resolved. The link itself lives
    on the site (D1), so Python may have no row for it.

    The /s/ page never checks the owner: it serves a link until the account
    deletion job deletes the link's row, and the job deletes the user in the
    same commit. So an owner whose account is closing (inactive, deletion
    pending) is served here too, and only an owner who no longer exists is
    refused, as the /s/ page refuses a link that doesn't exist."""
    try:
        uid = UUID(owner_id)
        if str(uid) != owner_id.lower():
            raise ValueError
    except ValueError:
        raise plain_error(400, "Invalid account ID") from None
    if await session.get(User, uid) is None:
        raise plain_error(404, "This search link doesn't exist.")
    return uid


def _error_response(request: Request, exc: Exception) -> Response:
    """The API's usual error shapes, kept out of caches like the successes.

    Litestar skips controller response_headers on exception responses, so
    resolve the handler by exception MRO, as site billing does."""
    handler = next(
        (
            BOT_API_EXCEPTION_HANDLERS[cls]
            for cls in type(exc).__mro__
            if cls in BOT_API_EXCEPTION_HANDLERS
        ),
        create_exception_response,
    )
    response = handler(request, exc)
    response.headers["Cache-Control"] = "private, no-store"
    if isinstance(exc, BotApiException) and exc.body.get("wait"):
        response.headers["Retry-After"] = str(exc.body["wait"])
    return response


def _detail(exc: HTTPException) -> str:
    return exc.detail if isinstance(exc.detail, str) else "Search unavailable"


class SiteSearchController(Controller):
    path = "/api/site/search"
    response_headers = {"Cache-Control": "private, no-store"}
    exception_handlers = {
        **dict.fromkeys(BOT_API_EXCEPTION_HANDLERS, _error_response),
        HTTPException: _error_response,
        Exception: _error_response,
    }

    @post(
        "/users/{user_id:str}/runs",
        guards=SITE_SEARCH_GUARDS,
        status_code=HTTP_201_CREATED,
    )
    async def start(
        self, db_session: AsyncSession, user_id: str, data: StartSearch
    ) -> dict[str, Any]:
        # The lock a forget of this key takes too: a start still on its way
        # when the site forgets it either lands first, and its run is the
        # forget's to delete, or finds the key forgotten.
        user = await _member(db_session, user_id, lock=True)
        key = _key(data.key)
        if await _forgotten(db_session, user.id, key):
            raise plain_error(410, "This search was deleted.")
        existing = await _run_for_key(db_session, user.id, key)
        if existing is not None:
            return {"search": site_snapshot(existing), "idempotent": True}
        try:
            text = dashboard._validate_request(
                data.request[: dashboard.MAX_REQUEST_CHARS]
            )
            await dashboard._enforce_limits(db_session, user.id)
        except HTTPException as exc:
            raise plain_error(exc.status_code, _detail(exc)) from None
        owner_id = user.id
        try:
            run = await dashboard.create_search(db_session, owner_id, text, key)
        except IntegrityError:
            # Two starts for the same turn raced and the other one saved its
            # run first (unique owner + key). Answer as an idempotent repeat.
            # The rollback expires ``user``; the lookup uses the saved id.
            await db_session.rollback()
            existing = await _run_for_key(db_session, owner_id, key)
            if existing is None:
                raise
            return {"search": site_snapshot(existing), "idempotent": True}
        return {"search": site_snapshot(run)}

    @get("/users/{user_id:str}/runs/{run_id:str}", guards=SITE_SEARCH_GUARDS)
    async def read(
        self, db_session: AsyncSession, user_id: str, run_id: str
    ) -> dict[str, Any]:
        user = await _member(db_session, user_id)
        return {"search": site_snapshot(await _owned(db_session, user, run_id))}

    @get("/users/{user_id:str}/keys/{key:str}", guards=SITE_SEARCH_GUARDS)
    async def by_key(
        self, db_session: AsyncSession, user_id: str, key: str
    ) -> dict[str, Any]:
        """The run a start with this key made, or ``{"search": null}`` when
        none did. For a start whose reply was lost: the site rejoins the run,
        or finds it to delete, without starting one. Never a 404 for a key
        with no run, so the site can tell this route from an API without it."""
        user = await _member(db_session, user_id)
        run = await _run_for_key(db_session, user.id, _key(key))
        return {"search": site_snapshot(run) if run is not None else None}

    @delete(
        "/users/{user_id:str}/keys/{key:str}",
        guards=SITE_SEARCH_GUARDS,
        status_code=200,
    )
    async def forget_key(
        self, db_session: AsyncSession, user_id: str, key: str
    ) -> dict[str, str]:
        """Forget the run a start with this key made, or will make: for a
        start whose reply was lost, so its id never reached the site.

        The key is marked forgotten first, under the member's user row lock
        that ``start`` takes too, so a start still on its way refuses (410).
        Then the run, if one was made, is deleted: 409 while it is still
        running, the mark kept, and the site asks again. Any member, closing
        accounts too; an account already gone has nothing left to forget.
        Done is 200 ``{"status": "forgotten"}``, never a 404, so the site
        tells it from an API without this route."""
        uid = _account_id(user_id)
        key = _key(key)
        user = await db_session.scalar(
            select(User).where(User.id == uid).with_for_update()
        )
        if user is None:
            return {"status": "forgotten"}
        if not await _forgotten(db_session, uid, key):
            db_session.add(WebSearchForgottenKey(owner_user_id=uid, submission_key=key))
            await db_session.flush()
        run = await _run_for_key(db_session, uid, key)
        if run is not None:
            try:
                await delete_search(db_session, run)
            except StillRunning:
                await db_session.commit()
                raise plain_error(
                    409, "Wait for this search to finish, then delete it."
                ) from None
        await db_session.commit()
        return {"status": "forgotten"}

    @delete(
        "/users/{user_id:str}/runs/{run_id:str}",
        guards=SITE_SEARCH_GUARDS,
        status_code=200,
    )
    async def remove(
        self, db_session: AsyncSession, user_id: str, run_id: str
    ) -> dict[str, str]:
        user = await _member(db_session, user_id)
        run = await _owned(db_session, user, run_id)
        try:
            await delete_search(db_session, run)
        except StillRunning:
            raise plain_error(
                409, "Wait for this search to finish, then delete it."
            ) from None
        return {"status": "deleted"}

    @get(
        "/users/{owner_id:str}/link-owner",
        guards=SITE_SEARCH_GUARDS,
        status_code=HTTP_204_NO_CONTENT,
    )
    async def link_owner(self, db_session: AsyncSession, owner_id: str) -> None:
        """Whether a search link's owner is still here. The site asks before
        every answer under /s/<token> (a prefetch, an empty question, the
        OpenSearch description included), as the /s/ page finds the link's
        row before anything else: yes (204) for an active or closing owner,
        404 once the deletion job has deleted them. It reads only, and says
        nothing that tells a closing owner from an active one."""
        await _owner_of(db_session, owner_id)

    @post(
        "/users/{owner_id:str}/anonymous-runs",
        guards=SITE_SEARCH_GUARDS,
        status_code=HTTP_201_CREATED,
    )
    async def start_anonymous(
        self, db_session: AsyncSession, owner_id: str, data: StartAnonymousSearch
    ) -> dict[str, Any]:
        owner = await _owner_of(db_session, owner_id)
        text = " ".join(data.request.split())[:MAX_REQUEST_CHARS]
        if not text:
            raise plain_error(422, "Describe what you're looking for.")
        redis = get_redis_client()
        refused = await anonymous.admit(
            redis, visitor=data.visitor, owner_user_id=owner
        )
        if refused:
            # per_link: the link's day ran out, which waiting a minute won't
            # fix (the /s/ page then shows no wait).
            raise BotApiException(
                429,
                {
                    "detail": refused.message,
                    "wait": refused.wait,
                    "per_link": refused.per_link,
                },
            )
        entry = await anonymous.launch(
            redis, owner_user_id=owner, nid=None, request=text
        )
        return {"search": entry["site"]}

    @get("/anonymous-runs/{search_id:str}", guards=SITE_SEARCH_GUARDS)
    async def read_anonymous(self, search_id: str) -> dict[str, Any]:
        try:
            sid = UUID(search_id)
        except ValueError:
            raise plain_error(404, "This search has expired.") from None
        entry = await anonymous.load(get_redis_client(), sid)
        # A search from the /s/ pages belongs to the browser that ran it.
        if entry is None or entry["nid"] is not None:
            raise plain_error(404, "This search has expired.")
        return {"search": entry["site"]}
