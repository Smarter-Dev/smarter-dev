"""Chat-agent memory preview for the Skrift admin panel.

One read-only page under ``/admin/bot/guilds/{guild_id}/chat-memory`` showing
all three layers of the chat agent's memory in one place: the long-term
per-guild blob the nightly dream rewrites, the mid-term notes the ``remember``
tool keeps during conversations, and the dream revision history kept for
diagnosis. The short-term layer — the conversation transcript itself — already
has a home in the conversations dashboard, so this page links out to it rather
than duplicating it.

Deliberately read-only. The per-guild pause switch (``memory_enabled``) is the
bot's contract with its guilds, and flipping it belongs next to the rest of the
guild-facing chat settings when it grows a UI; a preview page that silently
edits memory would be worse than none. The switch pauses memory, it never
erases it: the stored blob and notes stay as they are, the bot is not shown
them and the nightly dream skips the guild. It hides memory from the *bot*,
never from the admin: a disabled guild's blob still renders here, flagged,
because "what did it remember before we switched it off" is exactly the
question an operator with this page open is asking. Only the agent edits its
own memory, so this page has no edit or reset either.

The one thing it does switch is the public ``/chat-agent`` page (#103), which
never edits memory: each block sits beside its masked preview (#104), with
whatever the public check would hide flagged, so the admin reads it before
switching it on. Switching one guild on switches every other guild off.

And one narrow fix (#105): "tag this name" turns every whole-word occurrence
of a name the agent left bare in the three blocks into a member tag, with or
without a Discord id, and records a memory revision. Nothing else is editable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_error, flash_success, get_flash_messages
from skrift.forms.core import verify_csrf

from smarter_dev.web.chat_agent_public import check_public_blocks, set_public_page
from smarter_dev.web.chat_memory_retag import tag_name_in_blocks, tag_request_problem

from smarter_dev.web.api_native.chat_memory import utc_day_start
from smarter_dev.web.bot_admin.campaigns import fetch_guild_or_error
from smarter_dev.web.crud import (
    get_guild_memory_blob,
    list_guild_notes,
    list_memory_revisions,
)

logger = logging.getLogger(__name__)

_ACTIVE_PAGE = "chat_memory"
# How a memory column reads to the admin.
_BLOCK_LABELS = {"content": "memory"}


class ChatMemoryAdminController(Controller):
    """A guild's chat-agent memory under ``/admin/bot``: a view, the public-page
    switch and the one name-tagging fix."""

    path = "/admin/bot"
    guards = [auth_guard]

    @get(
        "/guilds/{guild_id:str}/chat-memory",
        guards=[auth_guard, Permission("administrator")],
    )
    async def chat_memory_view(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Render the guild's memory blob, notes, and dream history."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        blob = await get_guild_memory_blob(db_session, guild_id)
        notes = await list_guild_notes(db_session, guild_id)
        revisions = await list_memory_revisions(db_session, guild_id)
        # The agent's context window only includes notes from today (UTC); the
        # template uses this boundary to flag older survivors a failed dream
        # left behind.
        notes_since = utc_day_start(datetime.now(UTC))
        public_blocks = (
            await check_public_blocks(db_session, blob) if blob is not None else None
        )

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/chat_memory/view.html",
            context={
                "guild": guild,
                "blob": blob,
                "notes": notes,
                "revisions": revisions,
                "notes_since": notes_since,
                "public_blocks": public_blocks,
                "flash_messages": get_flash_messages(request),
                "active_page": _ACTIVE_PAGE,
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/chat-memory/public-page",
        guards=[auth_guard, Permission("administrator")],
    )
    async def chat_memory_public_page(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Switch the public /chat-agent page on for this guild, or off."""
        back = f"/admin/bot/guilds/{guild_id}/chat-memory"
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path=back)
        blob = await get_guild_memory_blob(db_session, guild_id)
        if blob is None:
            flash_error(request, "This guild has no memory yet, so there is nothing to show.")
            return Redirect(path=back)
        enabled = (await request.form()).get("enabled") == "on"
        await set_public_page(db_session, blob, enabled=enabled)
        await db_session.commit()
        flash_success(
            request,
            "The public page now shows this guild."
            if enabled
            else "The public page is off.",
        )
        return Redirect(path=back)

    @post(
        "/guilds/{guild_id:str}/chat-memory/tag-name",
        guards=[auth_guard, Permission("administrator")],
    )
    async def chat_memory_tag_name(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Tag every whole-word occurrence of a name in the three blocks."""
        back = f"/admin/bot/guilds/{guild_id}/chat-memory"
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path=back)
        form = await request.form()
        username = str(form.get("name") or "").strip()
        user_id = str(form.get("discord_id") or "").strip()
        problem = tag_request_problem(username, user_id)
        if problem is not None:
            flash_error(request, problem)
            return Redirect(path=back)
        result = await tag_name_in_blocks(
            db_session, guild_id, username, user_id or None
        )
        if not result.found:
            flash_error(
                request, "This guild has no memory yet, so there is nothing to tag."
            )
        elif result.over_cap:
            flash_error(
                request,
                "Tagging it would put "
                + ", ".join(_BLOCK_LABELS.get(c, c) for c in result.over_cap)
                + " over its length limit, so nothing changed.",
            )
        elif result.total == 0:
            flash_error(request, f"No block names {username} outside a tag.")
        else:
            logger.info(
                "Admin tagged %d bare name(s) in guild %s's chat memory",
                result.total,
                guild_id,
            )
            flash_success(
                request,
                f"Tagged {username} {result.total} time(s); "
                "the memory history has the change.",
            )
        return Redirect(path=back)
