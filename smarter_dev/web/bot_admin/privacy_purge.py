"""Admin pages for removing one Discord user from the chat bot.

A member asks the admin by DM; the admin opens ``/admin/bot/privacy-purges``,
enters the member's Discord user ID, looks up the names they went by, and
starts the purge. The request page shows each step as it lands — guild memory,
the bot's and the worker's history acknowledgements, and the final check — and
lets the admin run the purge or the check again, then close the request down
to a bare receipt.

A purge cannot start until every live process of both runtimes reports
enforcing the block list and each runtime has a live purge consumer:
otherwise their next wake could read the person's old messages straight back
into history, or nothing would purge their history. The page says which
runtime is missing.

The Discord user ID never goes in a URL: the name lookup is a POST, and a
refused start renders the page instead of redirecting with the ID.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC
from datetime import datetime
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar.exceptions import NotFoundException
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import get_flash_messages
from skrift.forms.core import verify_csrf
from sqlalchemy import desc
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.privacy_purge import SNOWFLAKE_PATTERN
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.chat_bot_purge import OUTSIDE_THE_CHECK
from smarter_dev.web.chat_bot_purge import STATUS_CLOSED
from smarter_dev.web.chat_bot_purge import affected_guild_ids
from smarter_dev.web.chat_bot_purge import block_list_revision
from smarter_dev.web.chat_bot_purge import clean_names
from smarter_dev.web.chat_bot_purge import close_request
from smarter_dev.web.chat_bot_purge import delete_commands
from smarter_dev.web.chat_bot_purge import open_purge_request
from smarter_dev.web.chat_bot_purge import possible_remains
from smarter_dev.web.chat_bot_purge import runtime_status
from smarter_dev.web.chat_bot_purge import start_new_run
from smarter_dev.web.chat_bot_purge import start_refusal
from smarter_dev.web.chat_bot_purge_jobs import submit_check
from smarter_dev.web.chat_bot_purge_jobs import submit_run
from smarter_dev.web.discord_admin_client import get_admin_discord_client
from smarter_dev.web.models import BytesTransaction
from smarter_dev.web.models import ChatBotPurgeRequest

logger = logging.getLogger(__name__)

BASE_PATH = "/admin/bot/privacy-purges"
_ACTIVE_PAGE = "privacy_purges"
_GUARDS = [auth_guard, Permission("administrator")]


async def known_names(db_session: AsyncSession, user_id: str) -> list[str]:
    """Names this user went by, from Discord and from what we stored."""
    names: list[str] = []
    try:
        client = get_admin_discord_client()
        names.extend(
            await client.get_user_names(user_id, await affected_guild_ids(db_session))
        )
    except Exception as error:  # noqa: BLE001 — the text can carry the ID
        logger.warning("Purge name lookup: Discord unavailable (%s)", type(error).__name__)
    rows = await db_session.execute(
        select(
            BytesTransaction.giver_id,
            BytesTransaction.giver_username,
            BytesTransaction.receiver_username,
        )
        .where(
            or_(
                BytesTransaction.giver_id == user_id,
                BytesTransaction.receiver_id == user_id,
            )
        )
        .limit(500)
    )
    for giver_id, giver_name, receiver_name in rows.all():
        names.append(giver_name if giver_id == user_id else receiver_name)
    return clean_names(names)


def _parse_names(raw: str) -> list[str]:
    return clean_names([line for line in raw.splitlines() if line.strip()])


def _valid_id(raw: object) -> str | None:
    user_id = str(raw or "").strip()
    return user_id if re.fullmatch(SNOWFLAKE_PATTERN, user_id) else None


async def _start_problem() -> str | None:
    return start_refusal(await runtime_status(get_redis_client()))


def _db_failure(action: str, error: Exception) -> None:
    # The type only: a database error's text quotes the statement's
    # parameters, which here are the Discord ID and names.
    logger.error("Privacy purge %s failed (%s)", action, type(error).__name__)


async def _list_page(
    request: Request,
    db_session: AsyncSession,
    *,
    lookup_id: str = "",
    names: list[str] | None = None,
) -> TemplateResponse:
    requests = (
        await db_session.scalars(
            select(ChatBotPurgeRequest)
            .order_by(desc(ChatBotPurgeRequest.created_at))
            .limit(100)
        )
    ).all()
    return TemplateResponse(
        "admin/bot/privacy_purges/list.html",
        context={
            "requests": requests,
            "lookup_id": lookup_id,
            "names": names or [],
            "runtimes": await runtime_status(get_redis_client()),
            "list_revision": await block_list_revision(db_session),
            "active_page": _ACTIVE_PAGE,
            "flash_messages": get_flash_messages(request),
            **await get_admin_context(request, db_session),
        },
    )


class PrivacyPurgeAdminController(Controller):
    path = BASE_PATH
    guards = [auth_guard]

    @get("", guards=_GUARDS)
    async def list_requests(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        return await _list_page(request, db_session)

    @post("/lookup", guards=_GUARDS)
    async def lookup(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse | Redirect:
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path=BASE_PATH)
        form = await request.form()
        user_id = _valid_id(form.get("user_id"))
        if user_id is None:
            flash_error(request, "A Discord user ID is 15 to 22 digits.")
            return Redirect(path=BASE_PATH)
        names = await known_names(db_session, user_id)
        return await _list_page(request, db_session, lookup_id=user_id, names=names)

    @post("", guards=_GUARDS)
    async def create(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse | Redirect:
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path=BASE_PATH)
        form = await request.form()
        user_id = _valid_id(form.get("user_id"))
        if user_id is None:
            flash_error(request, "A Discord user ID is 15 to 22 digits.")
            return Redirect(path=BASE_PATH)
        names = _parse_names(str(form.get("names") or ""))
        if problem := await _start_problem():
            # Rendered, not redirected: a redirect would carry the ID in its URL.
            flash_error(request, problem)
            return await _list_page(request, db_session, lookup_id=user_id, names=names)
        ctx = await get_admin_context(request, db_session)
        try:
            purge = await open_purge_request(
                db_session,
                discord_user_id=user_id,
                names=names,
                requested_by=str(ctx["user"].id) if ctx.get("user") else None,
            )
            request_id, run_id = purge.id, purge.run_id
            await db_session.commit()
        except SQLAlchemyError as error:
            await db_session.rollback()
            _db_failure("start", error)
            flash_error(request, "The purge could not be started. Please try again.")
            return await _list_page(request, db_session, lookup_id=user_id, names=names)
        await delete_commands(get_redis_client(), request_id=request_id, except_run=run_id)
        await submit_run(request_id, run_id)
        flash_success(request, "Purge started. This page shows each step as it lands.")
        return Redirect(path=f"{BASE_PATH}/{request_id}")

    @get("/{request_id:uuid}", guards=_GUARDS)
    async def view(
        self, request: Request, db_session: AsyncSession, request_id: UUID
    ) -> TemplateResponse:
        purge = await db_session.get(ChatBotPurgeRequest, request_id)
        if purge is None:
            raise NotFoundException()
        steps = purge.steps or {}
        return TemplateResponse(
            "admin/bot/privacy_purges/view.html",
            context={
                "purge": purge,
                "steps": steps,
                "report": purge.check_report or {},
                "possible_remains": possible_remains(steps),
                "outside_the_check": OUTSIDE_THE_CHECK,
                "runtimes": await runtime_status(get_redis_client()),
                "active_page": _ACTIVE_PAGE,
                "flash_messages": get_flash_messages(request),
                **await get_admin_context(request, db_session),
            },
        )

    @post("/{request_id:uuid}/rerun", guards=_GUARDS)
    async def rerun(
        self, request: Request, db_session: AsyncSession, request_id: UUID
    ) -> Redirect:
        back = Redirect(path=f"{BASE_PATH}/{request_id}")
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return back
        if problem := await _start_problem():
            flash_error(request, problem)
            return back
        try:
            purge = await db_session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
            if purge is None or purge.status == STATUS_CLOSED:
                flash_error(request, "A closed request cannot run again; start a new one.")
                return back
            start_new_run(purge, list_revision=await block_list_revision(db_session))
            run_id = purge.run_id
            await db_session.commit()
        except SQLAlchemyError as error:
            await db_session.rollback()
            _db_failure("re-run", error)
            flash_error(request, "The purge could not be started again. Please try again.")
            return back
        await delete_commands(get_redis_client(), request_id=request_id, except_run=run_id)
        await submit_run(request_id, run_id)
        flash_success(request, "Purge started again.")
        return back

    @post("/{request_id:uuid}/check", guards=_GUARDS)
    async def check(
        self, request: Request, db_session: AsyncSession, request_id: UUID
    ) -> Redirect:
        back = Redirect(path=f"{BASE_PATH}/{request_id}")
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return back
        await submit_check(request_id)
        flash_success(request, "Check started.")
        return back

    @post("/{request_id:uuid}/close", guards=_GUARDS)
    async def close(
        self, request: Request, db_session: AsyncSession, request_id: UUID
    ) -> Redirect:
        back = Redirect(path=f"{BASE_PATH}/{request_id}")
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return back
        try:
            await close_request(
                db_session, request_id, now=datetime.now(UTC), redis=get_redis_client()
            )
            await db_session.commit()
        except SQLAlchemyError as error:
            await db_session.rollback()
            _db_failure("close", error)
            flash_error(request, "The request could not be closed. Please try again.")
            return back
        flash_success(
            request,
            "Closed. The request is now a bare receipt; the user stays on the block list.",
        )
        return back
