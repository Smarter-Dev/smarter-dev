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
from smarter_dev.web.models import WebSearchLink
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


async def _member(session: AsyncSession, user_id: str) -> User:
    try:
        uid = UUID(user_id)
        if str(uid) != user_id.lower():
            raise ValueError
    except ValueError:
        raise plain_error(400, "Invalid account ID") from None
    user = await session.scalar(
        select(User).where(User.id == uid, User.is_active.is_(True))
    )
    if user is None:
        raise plain_error(404, "Account not found")
    return user


async def _owned(session: AsyncSession, user: User, run_id: str) -> WebSearchRun:
    try:
        rid = UUID(run_id)
    except ValueError:
        raise plain_error(404, "Search not found") from None
    run = await session.get(WebSearchRun, rid)
    if run is None or run.owner_user_id != user.id:
        raise plain_error(404, "Search not found")
    return run


async def _link_of(session: AsyncSession, owner_id: str) -> tuple[UUID, str]:
    """The active owner and the daily count of their search link: the Python
    link's id, shared with the /s/ page, or a key per owner for a link that
    only the site has. An unknown or inactive owner is refused as the /s/
    page refuses a link that doesn't exist."""
    try:
        uid = UUID(owner_id)
        if str(uid) != owner_id.lower():
            raise ValueError
    except ValueError:
        raise plain_error(400, "Invalid account ID") from None
    owner = await session.get(User, uid)
    if owner is None or not owner.is_active:
        raise plain_error(404, "This search link doesn't exist.")
    link_id = await session.scalar(
        select(WebSearchLink.id).where(WebSearchLink.owner_user_id == uid)
    )
    return uid, str(link_id) if link_id is not None else anonymous.owner_link(uid)


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
        user = await _member(db_session, user_id)
        try:
            key = dashboard._validate_submission_key(data.key)
        except HTTPException as exc:
            raise plain_error(422, _detail(exc)) from None
        existing = await db_session.scalar(
            select(WebSearchRun).where(
                WebSearchRun.owner_user_id == user.id,
                WebSearchRun.submission_key == key,
            )
        )
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
            existing = await db_session.scalar(
                select(WebSearchRun).where(
                    WebSearchRun.owner_user_id == owner_id,
                    WebSearchRun.submission_key == key,
                )
            )
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

    @post(
        "/users/{owner_id:str}/anonymous-runs",
        guards=SITE_SEARCH_GUARDS,
        status_code=HTTP_201_CREATED,
    )
    async def start_anonymous(
        self, db_session: AsyncSession, owner_id: str, data: StartAnonymousSearch
    ) -> dict[str, Any]:
        owner, link_id = await _link_of(db_session, owner_id)
        text = " ".join(data.request.split())[:MAX_REQUEST_CHARS]
        if not text:
            raise plain_error(422, "Describe what you're looking for.")
        redis = get_redis_client()
        refused = await anonymous.admit(redis, visitor=data.visitor, link_id=link_id)
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
