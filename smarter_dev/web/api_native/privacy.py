"""Bot API for the chat bot purge: the block list and the runtimes' acks.

- ``GET  /api/privacy/blocked-users`` → the Discord IDs neither runtime may
  read, and the list's revision. It is a list of people who asked to be
  deleted, so it sits behind the bot-API key like every other route here and
  is never public.
- ``POST /api/privacy/purges/{run_id}/acks`` → one runtime's result for one
  guild of a purge run. 404 for a run that is unknown or was superseded by a
  newer submit, which tells the runtime to drop its command. The last ack of a
  run submits the check.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller
from litestar import get
from litestar import post
from litestar.status_codes import HTTP_200_OK
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.privacy_purge import BlockedUsers
from smarter_dev.shared.privacy_purge import PurgeAck
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import nested_not_found_error
from smarter_dev.web.chat_bot_purge import STATUS_CHECKING
from smarter_dev.web.chat_bot_purge import read_blocked_users
from smarter_dev.web.chat_bot_purge import record_ack
from smarter_dev.web.chat_bot_purge_jobs import submit_check

BOT_API_PERMISSION = "bot-api"
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class PrivacyController(Controller):
    path = "/api/privacy"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/blocked-users", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def blocked_users(self, db_session: AsyncSession) -> BlockedUsers:
        return await read_blocked_users(db_session)

    @post("/purges/{run_id:uuid}/acks", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def acknowledge(
        self, db_session: AsyncSession, run_id: UUID, data: PurgeAck
    ) -> dict:
        request = await record_ack(db_session, run_id, data)
        if request is None:
            raise nested_not_found_error(f"Purge run '{run_id}' not found")
        ready_for_check = request.status == STATUS_CHECKING
        request_id = request.id
        await db_session.commit()
        if ready_for_check:
            await submit_check(request_id)
        return {"accepted": True}
