"""Admin pages for removing one Discord user from the chat bot.

A member asks the admin by DM; the admin opens ``/admin/bot/privacy-purges``,
enters the member's Discord user ID, looks up the names they went by, and
starts the purge. The request page shows each step as it lands — guild memory,
the bot's and the worker's history acknowledgements, and the final check — and
lets the admin run the purge or the check again, then close the request down
to a bare receipt.

A purge cannot start until both runtimes report enforcing the block list:
otherwise their next wake could read the person's old messages straight back
into history. The page says which runtime is missing.
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
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.privacy_purge import SNOWFLAKE_PATTERN
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.chat_bot_purge import STATUS_CLOSED
from smarter_dev.web.chat_bot_purge import affected_guild_ids
from smarter_dev.web.chat_bot_purge import all_enforcing
from smarter_dev.web.chat_bot_purge import block_list_revision
from smarter_dev.web.chat_bot_purge import clean_names
from smarter_dev.web.chat_bot_purge import close_request
from smarter_dev.web.chat_bot_purge import open_purge_request
from smarter_dev.web.chat_bot_purge import runtimes_enforcing
from smarter_dev.web.chat_bot_purge import start_new_run
from smarter_dev.web.chat_bot_purge_jobs import submit_check
from smarter_dev.web.chat_bot_purge_jobs import submit_run
from smarter_dev.web.discord_admin_client import DiscordAdminError
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
    except DiscordAdminError:
        logger.warning("Purge name lookup: Discord unavailable")
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


async def _enforcing_problem(db_session: AsyncSession) -> str | None:
    enforcing = await runtimes_enforcing(get_redis_client())
    if all_enforcing(enforcing):
        return None
    missing = ", ".join(name for name, value in enforcing.items() if value is None)
    return (
        f"Not enforcing the block list yet: {missing}. A purge started now could be "
        "undone by the next wake, so it is not started. Deploy or restart that "
        "runtime, wait a minute and try again."
    )


class PrivacyPurgeAdminController(Controller):
    path = BASE_PATH
    guards = [auth_guard]

    @get("", guards=_GUARDS)
    async def list_requests(
        self, request: Request, db_session: AsyncSession, user_id: str | None = None
    ) -> TemplateResponse:
        requests = (
            await db_session.scalars(
                select(ChatBotPurgeRequest)
                .order_by(desc(ChatBotPurgeRequest.created_at))
                .limit(100)
            )
        ).all()
        lookup_id = (user_id or "").strip()
        names: list[str] = []
        if lookup_id and re.fullmatch(SNOWFLAKE_PATTERN, lookup_id):
            names = await known_names(db_session, lookup_id)
        elif lookup_id:
            flash_error(request, "A Discord user ID is 15 to 22 digits.")
        return TemplateResponse(
            "admin/bot/privacy_purges/list.html",
            context={
                "requests": requests,
                "lookup_id": lookup_id,
                "names": names,
                "enforcing": await runtimes_enforcing(get_redis_client()),
                "list_revision": await block_list_revision(db_session),
                "active_page": _ACTIVE_PAGE,
                "flash_messages": get_flash_messages(request),
                **await get_admin_context(request, db_session),
            },
        )

    @post("", guards=_GUARDS)
    async def create(self, request: Request, db_session: AsyncSession) -> Redirect:
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path=BASE_PATH)
        form = await request.form()
        user_id = str(form.get("user_id") or "").strip()
        if not re.fullmatch(SNOWFLAKE_PATTERN, user_id):
            flash_error(request, "A Discord user ID is 15 to 22 digits.")
            return Redirect(path=BASE_PATH)
        if problem := await _enforcing_problem(db_session):
            flash_error(request, problem)
            return Redirect(path=f"{BASE_PATH}?user_id={user_id}")
        ctx = await get_admin_context(request, db_session)
        purge = await open_purge_request(
            db_session,
            discord_user_id=user_id,
            names=_parse_names(str(form.get("names") or "")),
            requested_by=str(ctx["user"].id) if ctx.get("user") else None,
        )
        request_id, run_id = purge.id, purge.run_id
        await db_session.commit()
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
        return TemplateResponse(
            "admin/bot/privacy_purges/view.html",
            context={
                "purge": purge,
                "steps": purge.steps or {},
                "report": purge.check_report or {},
                "enforcing": await runtimes_enforcing(get_redis_client()),
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
        purge = await db_session.get(ChatBotPurgeRequest, request_id, with_for_update=True)
        if purge is None or purge.status == STATUS_CLOSED:
            flash_error(request, "A closed request cannot run again; start a new one.")
            return back
        if problem := await _enforcing_problem(db_session):
            flash_error(request, problem)
            return back
        start_new_run(purge, list_revision=await block_list_revision(db_session))
        run_id = purge.run_id
        await db_session.commit()
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
        await close_request(
            db_session, request_id, now=datetime.now(UTC), redis=get_redis_client()
        )
        await db_session.commit()
        flash_success(
            request,
            "Closed. The request is now a bare receipt; the user stays on the block list.",
        )
        return back
