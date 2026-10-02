"""A user's search link, ``/s/<token>?q=…``, added to a browser as a search engine.

The token picks the link and its options; it never signs anyone in.

- With "open web addresses" on, a request Jev judges to be a web address
  (``address.address_to_open``) redirects there instead of searching, but
  only for the link's owner, logged in on that browser.
- A visitor logged in on that browser gets a saved search, as if they had
  typed it on the dashboard, under the dashboard's limits.
- Anyone else gets an anonymous search (``web_search.anonymous``): never
  saved, shown only to the browser that ran it, two a minute per visitor, and
  after that a page asking them to wait or log in.

Browsers prefetch and prerender likely results pages; those requests
(``Sec-Purpose: prefetch``) start nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID
from uuid import uuid4

from litestar import Controller
from litestar import Request
from litestar import Response
from litestar import get
from litestar.background_tasks import BackgroundTask
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import Redirect
from litestar.response import Template
from skrift.hooks import APP_SHUTDOWN
from skrift.hooks import APP_STARTUP
from skrift.hooks import action
from skrift.lib.client_ip import get_client_ip
from skrift.notifications import ensure_nid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web import dashboard_controller as dashboard
from smarter_dev.web.models import WebSearchLink
from smarter_dev.web.web_search import anonymous
from smarter_dev.web.web_search.snapshot import EVENT_TYPE
from smarter_dev.web.web_search.snapshot import MAX_REQUEST_CHARS

logger = logging.getLogger(__name__)

OPENSEARCH = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>Smarter Dev</ShortName>
  <Description>Search the web with Luna and Jev</Description>
  <InputEncoding>UTF-8</InputEncoding>
  <Image width="1000" height="1000" type="image/png">{base}/static/site/favicon.png</Image>
  <Url type="text/html" method="get" template="{base}/s/{token}?q={{searchTerms}}"/>
</OpenSearchDescription>
"""


def _is_prefetch(request: Request) -> bool:
    purpose = request.headers.get("sec-purpose") or request.headers.get("purpose") or ""
    return "prefetch" in purpose.lower()


def _login_url(request: Request) -> str:
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"/auth/login?{urlencode({'next': here})}"


def _limited(request: Request, *, message: str, wait: int | None, login: bool) -> Template:
    return Template(
        "dashboard/search-limited.html",
        context={
            "message": message,
            "wait": wait,
            "login_url": _login_url(request) if login else None,
            "retry_url": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            "seo_meta": {"robots": "noindex,nofollow"},
        },
        status_code=429,
        headers={"Cache-Control": "no-store"},
    )


# Set on the website pods only: tests and other processes skip the warm-up.
WARM_ON_STARTUP = os.getenv("WEB_SEARCH_ADDRESS_WARM") == "1"
WARM_TIMEOUT_SECONDS = 60


@action(APP_STARTUP)
async def warm_address_check(_app) -> None:
    """Load Jev's SDK before the pod takes traffic (see ``address.warm``)."""
    if not WARM_ON_STARTUP:
        return
    from smarter_dev.web.web_search import address

    started = time.monotonic()
    try:
        await asyncio.wait_for(address.warm(), WARM_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 - a cold first check beats a pod that won't start
        logger.warning("Search link address check warm-up failed (%s)", type(error).__name__)
        return
    logger.info("Search link address check warmed in %d ms", (time.monotonic() - started) * 1000)


@action(APP_SHUTDOWN)
async def close_address_client(_app) -> None:
    from smarter_dev.web.web_search import address

    await address.close_shared_client()


async def _check_address(text: str) -> tuple[str | None, dict]:
    """The URL to open, or None to search; and Jev's usage to record."""
    from smarter_dev.web.web_search.address import address_to_open

    started = time.monotonic()
    try:
        url, usage = await address_to_open(text)
    except Exception as error:  # noqa: BLE001 - searching is the safe fallback
        logger.warning("Search link address check failed (%s)", type(error).__name__)
        return None, {}
    # Timing and outcome only: the text is the user's.
    logger.info(
        "Search link address check: %s in %d ms (%s)",
        "open" if url else "search",
        (time.monotonic() - started) * 1000,
        "jev" if usage else "no jev",
    )
    return url, usage


async def _start_anonymous(request: Request, link: WebSearchLink, text: str) -> Response:
    redis = get_redis_client()
    wait = await anonymous.wait_seconds(
        redis,
        f"web_search:anonymous:visitor:{get_client_ip(request.scope)}",
        anonymous.PER_MINUTE_LIMIT,
        60,
    )
    if wait:
        return _limited(
            request,
            message=(
                f"Searches without logging in are limited to {anonymous.PER_MINUTE_LIMIT} a minute."
            ),
            wait=wait,
            login=True,
        )
    if await anonymous.wait_seconds(
        redis,
        f"web_search:anonymous:link:{link.id}",
        anonymous.LINK_PER_DAY_LIMIT,
        86_400,
    ):
        return _limited(
            request,
            message="This search link has run all its searches without logging in for today.",
            wait=None,
            login=True,
        )
    state = await anonymous.start(
        redis, owner_user_id=link.owner_user_id, nid=ensure_nid(request), request=text
    )
    from skrift.workers import submit as worker_submit

    from smarter_dev.web.chat.dispatch import ensure_submission_handler

    try:
        ensure_submission_handler(anonymous.JOB_TYPE)
        await worker_submit(
            anonymous.JOB_TYPE,
            {"search_id": state["id"]},
            queue="agents",
            job_id=f"anonymous:{state['id']}",
        )
    except Exception as error:  # noqa: BLE001 - shown on the search page
        logger.error("Anonymous web search couldn't start (%s)", type(error).__name__)
        entry = await anonymous.load(redis, state["id"])
        if entry is not None:
            entry["search"].update(
                status="error", active=False, error="Couldn't start the search. Try again."
            )
            await anonymous.save(redis, entry)
    return Redirect(path=f"/s/r/{state['id']}", status_code=303)


async def _visible(request: Request, search_id: UUID) -> dict | None:
    """The anonymous search, if it was started from this browser session."""
    entry = await anonymous.load(get_redis_client(), search_id)
    if entry is None or entry["nid"] != ensure_nid(request):
        return None
    return entry["search"]


class SearchLinkController(Controller):
    path = "/s"

    @get("/{token:str}")
    async def run(
        self,
        request: Request,
        db_session: AsyncSession,
        token: str,
        q: Annotated[str | None, Parameter(query="q")] = None,
    ) -> Response:
        received = time.monotonic()
        link = await db_session.scalar(select(WebSearchLink).where(WebSearchLink.token == token))
        if link is None:
            raise HTTPException(status_code=404, detail="This search link doesn't exist.")
        if _is_prefetch(request):
            return Response(content=b"", status_code=204, headers={"Cache-Control": "no-store"})
        text = " ".join((q or "").split())[:MAX_REQUEST_CHARS]
        if not text:
            return Redirect(path="/dashboard/search", status_code=303)

        user_id = dashboard._session_user_id(request)
        user = await db_session.get(dashboard.User, user_id) if user_id else None
        if user is not None and not user.is_active:
            user = None

        # Only the link's owner is redirected: anyone could otherwise make a
        # link that turns smarter.dev into an open redirect for phishing.
        if link.open_addresses and user is not None and user.id == link.owner_user_id:
            from smarter_dev.web.web_search import metering

            url, usage = await _check_address(text)
            if url is not None:
                logger.info(
                    "Search link redirect ready in %d ms", (time.monotonic() - received) * 1000
                )
                # The ledger row is written after the redirect goes out.
                return Redirect(
                    path=url,
                    status_code=302,
                    background=BackgroundTask(metering.record_address, link.owner_user_id, usage)
                    if usage
                    else None,
                )
            if usage:
                await metering.record_address(link.owner_user_id, usage)

        if user is None:
            return await _start_anonymous(request, link, text)
        try:
            await dashboard._enforce_limits(db_session, user.id)
        except HTTPException as error:
            return _limited(request, message=str(error.detail), wait=None, login=False)
        run = await dashboard.create_search(db_session, user.id, text, uuid4().hex)
        return Redirect(path=f"/dashboard/search/{run.id}", status_code=303)

    @get("/{token:str}/opensearch.xml")
    async def opensearch(self, db_session: AsyncSession, token: str) -> Response:
        link = await db_session.scalar(select(WebSearchLink).where(WebSearchLink.token == token))
        if link is None:
            raise HTTPException(status_code=404, detail="This search link doesn't exist.")
        base = get_settings().site_base_url.rstrip("/")
        return Response(
            content=OPENSEARCH.format(base=base, token=link.token),
            media_type="application/opensearchdescription+xml",
            headers={"Cache-Control": "no-store"},
        )

    @get("/r/{search_id:uuid}")
    async def anonymous_page(self, request: Request, search_id: UUID) -> Template:
        search = await _visible(request, search_id)
        if search is None:
            raise HTTPException(
                status_code=404,
                detail="Searches you run without logging in last 30 minutes and only in the browser that ran them.",
            )
        return Template(
            "dashboard/index.html",
            context={
                "dashboard_state": {
                    "view": "search",
                    "anonymous": True,
                    "user": None,
                    "recent": [],
                    "search": search,
                    "search_api": "/s/api/",
                    "login_url": f"/auth/login?{urlencode({'next': '/dashboard/search'})}",
                    "event_type": EVENT_TYPE,
                    "max_request_chars": MAX_REQUEST_CHARS,
                    "build": dashboard.CLIENT_BUILD,
                    "link": None,
                },
                "seo_meta": {"robots": "noindex,nofollow"},
            },
            headers={"Cache-Control": "no-store"},
        )

    @get("/api/{search_id:uuid}")
    async def anonymous_search(self, request: Request, search_id: UUID) -> dict:
        search = await _visible(request, search_id)
        if search is None:
            raise HTTPException(status_code=404, detail="This search has expired.")
        return {"search": search, "build": dashboard.CLIENT_BUILD}
