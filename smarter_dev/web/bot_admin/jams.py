"""Code jam management for the Skrift admin panel.

Pages under ``/admin/bot/guilds/{guild_id}/jams``: create and edit a jam, run
its Discord setup, and schedule messages into its channels. This module is
only the pages. What a jam is and how it is stored live in
:mod:`smarter_dev.web.jams.store`; creating the Discord category, channels and
role lives in :mod:`smarter_dev.web.jams.discord_setup`.

Form reading and validation are pure module-level helpers, like the sibling
repeating-message controller, so they can be tested without a request.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Protocol
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Response
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import get_flash_messages
from skrift.forms.core import verify_csrf
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.bot_admin.campaigns import fetch_guild_or_error
from smarter_dev.web.bot_admin.campaigns import parse_campaign_datetime
from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import DiscordChannel
from smarter_dev.web.discord_admin_client import get_admin_discord_client
from smarter_dev.web.jams import results
from smarter_dev.web.jams import store
from smarter_dev.web.jams import teams
from smarter_dev.web.jams.discord_setup import CATEGORY_NAME
from smarter_dev.web.jams.discord_setup import CHANNEL_NAMES
from smarter_dev.web.jams.discord_setup import DISCORD_ERRORS
from smarter_dev.web.jams.discord_setup import ROLE_NAME
from smarter_dev.web.jams.discord_setup import JamSetupReport
from smarter_dev.web.jams.discord_setup import run_jam_setup
from smarter_dev.web.models import JAM_CHANNEL_COLUMNS
from smarter_dev.web.models import JAM_MESSAGE_CHANNEL_KEYS
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamDiscordSetup
from smarter_dev.web.models import JamMessage

logger = logging.getLogger(__name__)

_CREATED_BY = "admin"
_FORUM = 15
_ACTIVE_PAGE = "jams"
_ADMIN = [auth_guard, Permission("administrator")]

_NAME_MAX = 100
# Discord's limit for a forum tag name.
_TAG_NAME_MAX = 20
_TEAM_SIZE_MAX = 20
# Discord's limit is 2000; the role mention the bot may put in front takes
# up to 26 characters.
MESSAGE_CONTENT_MAX = 1900

_DATETIME_FIELDS = {
    "starts_at": "Start time",
    "submissions_close_at": "Submission deadline",
    "voting_ends_at": "Voting end",
    "winners_at": "Winners date",
}
_REQUIRED_DATETIMES = ("starts_at", "submissions_close_at")


class FormLike(Protocol):
    """The subset of the submitted-form interface the readers rely on."""

    def get(self, key: str, default: Any = ...) -> Any: ...


def _text(form: FormLike, key: str) -> str:
    return (form.get(key) or "").strip()


def _local(moment: datetime | None) -> str:
    """A stored datetime as a ``datetime-local`` input value (UTC)."""
    return moment.strftime("%Y-%m-%dT%H:%M") if moment else ""


# -- Jam form -----------------------------------------------------------------


def read_jam_form(form: FormLike) -> dict:
    """Extract a submitted jam form into a raw field dict. Pure."""
    return {
        "name": _text(form, "name"),
        "tag_name": _text(form, "tag_name"),
        "max_team_size": _text(form, "max_team_size"),
        **{field: _text(form, field) for field in _DATETIME_FIELDS},
    }


def jam_form_data(jam: Jam) -> dict:
    """A stored jam as the raw field dict the form renders from."""
    return {
        "name": jam.name,
        "tag_name": jam.tag_name,
        "max_team_size": str(jam.max_team_size),
        **{field: _local(getattr(jam, field)) for field in _DATETIME_FIELDS},
    }


def validate_jam_form(data: dict) -> tuple[bool, list[str], dict]:
    """Validate a raw jam form dict. Pure; returns ``(ok, errors, cleaned)``."""
    errors: list[str] = []

    name = str(data.get("name", "")).strip()
    if not name:
        errors.append("Name is required")
    elif len(name) > _NAME_MAX:
        errors.append(f"Name must be {_NAME_MAX} characters or fewer")

    tag_name = str(data.get("tag_name", "")).strip()
    if not tag_name:
        errors.append("Showcase tag is required")
    elif len(tag_name) > _TAG_NAME_MAX:
        errors.append(f"Showcase tag must be {_TAG_NAME_MAX} characters or fewer")
    elif tag_name.isdigit():
        # A number would collide with the year tags.
        errors.append("Showcase tag cannot be only digits")

    max_team_size: int | None = None
    try:
        max_team_size = int(str(data.get("max_team_size", "")).strip())
    except ValueError:
        errors.append("Team size must be a whole number")
    else:
        if not 1 <= max_team_size <= _TEAM_SIZE_MAX:
            errors.append(f"Team size must be between 1 and {_TEAM_SIZE_MAX}")

    moments: dict[str, datetime | None] = {}
    for field, label in _DATETIME_FIELDS.items():
        raw = str(data.get(field, "")).strip()
        moments[field] = None
        if not raw:
            if field in _REQUIRED_DATETIMES:
                errors.append(f"{label} is required")
            continue
        try:
            moments[field] = parse_campaign_datetime(raw)
        except (ValueError, TypeError):
            errors.append(f"{label} is not a valid date and time")

    # Each date that is set must come after the one before it.
    previous_label, previous = None, None
    for field, label in _DATETIME_FIELDS.items():
        moment = moments[field]
        if moment is None:
            continue
        if previous is not None and moment <= previous:
            errors.append(f"{label} must be after the {previous_label.lower()}")
        previous_label, previous = label, moment

    cleaned = {
        "name": name,
        "tag_name": tag_name,
        "max_team_size": max_team_size,
        **moments,
    }
    return not errors, errors, cleaned


# -- Message form -------------------------------------------------------------


def read_jam_message_form(form: FormLike) -> dict:
    """Extract a submitted jam-message form into a raw field dict. Pure."""
    return {
        "channel_key": _text(form, "channel_key"),
        "content": _text(form, "content"),
        "send_at": _text(form, "send_at"),
        "mention_role": bool(form.get("mention_role")),
    }


def jam_message_form_data(message: JamMessage) -> dict:
    """A stored message as the raw field dict the form renders from."""
    return {
        "channel_key": message.channel_key,
        "content": message.content,
        "send_at": _local(message.send_at),
        "mention_role": message.mention_role,
    }


def validate_jam_message_form(data: dict) -> tuple[bool, list[str], dict]:
    """Validate a raw jam-message dict. Pure; returns ``(ok, errors, cleaned)``."""
    errors: list[str] = []

    channel_key = str(data.get("channel_key", "")).strip()
    if channel_key not in JAM_MESSAGE_CHANNEL_KEYS:
        errors.append("Choose one of the jam's channels")

    content = str(data.get("content", "")).strip()
    if not content:
        errors.append("Message is required")
    elif len(content) > MESSAGE_CONTENT_MAX:
        errors.append(f"Message must be {MESSAGE_CONTENT_MAX} characters or fewer")

    send_at: datetime | None = None
    raw_send_at = str(data.get("send_at", "")).strip()
    if not raw_send_at:
        errors.append("Send time is required")
    else:
        try:
            send_at = parse_campaign_datetime(raw_send_at)
        except (ValueError, TypeError):
            errors.append("Send time is not a valid date and time")

    cleaned = {
        "channel_key": channel_key,
        "content": content,
        "send_at": send_at,
        "mention_role": bool(data.get("mention_role")),
    }
    return not errors, errors, cleaned


# -- Controller ---------------------------------------------------------------


def _jams_path(guild_id: str) -> str:
    return f"/admin/bot/guilds/{guild_id}/jams"


def _jam_path(guild_id: str, jam_id: UUID) -> str:
    return f"{_jams_path(guild_id)}/{jam_id}"


class JamsAdminController(Controller):
    """Jam pages for a guild under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    @get("/guilds/{guild_id:str}/jams", guards=_ADMIN)
    async def jams_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """List a guild's jams."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        return await render(
            request,
            db_session,
            "admin/bot/jams/list.html",
            guild_id,
            guild=guild,
            jams=await store.list_jams(db_session, guild_id),
            setup=await store.get_setup(db_session, guild_id),
            forums=await list_forums(guild_id),
            default_forum_name=CHANNEL_NAMES["showcase"],
            flash_messages=get_flash_messages(request),
        )

    @post("/guilds/{guild_id:str}/jams/showcase-forum", guards=_ADMIN)
    async def jams_showcase_forum(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Choose which forum jam entries are posted in."""
        if (refused := await csrf_refusal(request, _jams_path(guild_id))) is not None:
            return refused
        form = await request.form()
        channel_id = _text(form, "channel_id")
        if channel_id and channel_id not in {f.id for f in await list_forums(guild_id)}:
            flash_error(request, "That forum was not found in this server.")
            return Redirect(path=_jams_path(guild_id))
        async with store.guild_lock(db_session, guild_id):
            await store.set_showcase_forum(db_session, guild_id, channel_id or None)
            await db_session.commit()
        flash_success(
            request,
            "Showcase forum saved. Press Finish setup on a jam to add its tags there.",
        )
        return Redirect(path=_jams_path(guild_id))

    @get("/guilds/{guild_id:str}/jams/create", guards=_ADMIN)
    async def jam_create_form(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Render the blank create-jam form."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        return await render_jam_form(
            request, db_session, guild, guild_id, None, None, []
        )

    @post("/guilds/{guild_id:str}/jams/create", guards=_ADMIN)
    async def jam_create(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Create a jam, then whatever it needs in Discord that is missing."""
        if (refused := await csrf_refusal(request, _jams_path(guild_id))) is not None:
            return refused
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        data = read_jam_form(await request.form())
        is_valid, errors, cleaned = validate_jam_form(data)
        jam = None
        if is_valid:
            # Checked and saved under the guild's lock, so a second click
            # sees the first jam and is refused as an overlap.
            async with store.guild_lock(db_session, guild_id):
                errors = await conflicts(db_session, guild_id, cleaned, None)
                if not errors:
                    jam = Jam(guild_id=guild_id, created_by=_CREATED_BY, **cleaned)
                    db_session.add(jam)
                # Saved before Discord is touched: if setup fails part way
                # the jam is still there, and its page offers to finish it.
                await db_session.commit()
        if jam is None:
            return await render_jam_form(
                request, db_session, guild, guild_id, None, data, errors, 400
            )

        report = await set_up_discord(db_session, jam)
        flash_setup_report(request, report, f"Jam “{jam.name}” created.")
        return Redirect(path=_jam_path(guild_id, jam.id))

    @get("/guilds/{guild_id:str}/jams/{jam_id:uuid}", guards=_ADMIN)
    async def jam_detail(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Show a jam: its Discord setup and its scheduled messages."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is None:
            return await render_not_found(request, db_session, guild_id, "Jam")
        return await render_jam_detail(request, db_session, guild, jam, None, [])

    @get("/guilds/{guild_id:str}/jams/{jam_id:uuid}/edit", guards=_ADMIN)
    async def jam_edit_form(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Render the edit form for a jam."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is None:
            return await render_not_found(request, db_session, guild_id, "Jam")
        return await render_jam_form(
            request, db_session, guild, guild_id, jam, jam_form_data(jam), []
        )

    @post("/guilds/{guild_id:str}/jams/{jam_id:uuid}/edit", guards=_ADMIN)
    async def jam_edit(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Apply edits to a jam. Nothing in Discord is renamed or removed."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is None:
            return await render_not_found(request, db_session, guild_id, "Jam")

        data = read_jam_form(await request.form())
        is_valid, errors, cleaned = validate_jam_form(data)
        if is_valid:
            async with store.guild_lock(db_session, guild_id):
                await db_session.refresh(jam)
                errors = await conflicts(db_session, guild_id, cleaned, jam)
                if not errors:
                    apply_jam_edit(jam, cleaned)
                await db_session.commit()
        if errors:
            return await render_jam_form(
                request, db_session, guild, guild_id, jam, data, errors, 400
            )
        flash_success(request, "Jam updated.")
        if jam.missing_tags:
            flash_error(
                request,
                "The jam's showcase tags are not all created. Press Finish setup.",
            )
        return Redirect(path=_jam_path(guild_id, jam.id))

    @post("/guilds/{guild_id:str}/jams/{jam_id:uuid}/setup", guards=_ADMIN)
    async def jam_setup(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Create whichever Discord pieces the jam needs and does not have."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is None:
            flash_error(request, "Jam not found.")
            return Redirect(path=_jams_path(guild_id))
        report = await set_up_discord(db_session, jam)
        flash_setup_report(request, report, "Discord setup is complete.")
        return Redirect(path=_jam_path(guild_id, jam.id))

    @post("/guilds/{guild_id:str}/jams/{jam_id:uuid}/delete", guards=_ADMIN)
    async def jam_delete(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Delete a jam and its scheduled messages. Discord is left as it is."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        # Under the guild's lock, so a setup run in progress finishes first.
        async with store.guild_lock(db_session, guild_id):
            jam = await store.get_jam(db_session, guild_id, jam_id)
            if jam is None:
                flash_error(request, "Jam not found.")
                return Redirect(path=_jams_path(guild_id))
            await db_session.delete(jam)
            await db_session.commit()
        flash_success(
            request,
            "Jam deleted. Nothing was removed from Discord.",
        )
        return Redirect(path=_jams_path(guild_id))

    # -- Messages -------------------------------------------------------------

    @post("/guilds/{guild_id:str}/jams/{jam_id:uuid}/messages/create", guards=_ADMIN)
    async def jam_message_create(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Schedule a message into one of the jam's channels."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is None:
            return await render_not_found(request, db_session, guild_id, "Jam")

        data = read_jam_message_form(await request.form())
        is_valid, errors, cleaned = validate_jam_message_form(data)
        if not is_valid:
            return await render_jam_detail(
                request, db_session, guild, jam, data, errors, 400
            )

        db_session.add(JamMessage(jam_id=jam.id, created_by=_CREATED_BY, **cleaned))
        await db_session.commit()
        flash_success(request, "Message scheduled.")
        return Redirect(path=_jam_path(guild_id, jam.id))

    @get(
        "/guilds/{guild_id:str}/jams/{jam_id:uuid}/messages/{message_id:uuid}/edit",
        guards=_ADMIN,
    )
    async def jam_message_edit_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        jam_id: UUID,
        message_id: UUID,
    ) -> Response:
        """Render the edit form for a message that has not been sent."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        jam, message = await load_message(db_session, guild_id, jam_id, message_id)
        if message is None:
            return await render_not_found(request, db_session, guild_id, "Message")
        if message.status not in ("pending", "failed"):
            flash_error(request, "Only a message that has not been sent can be edited.")
            return Redirect(path=_jam_path(guild_id, jam_id))
        return await render_message_form(
            request, db_session, guild, jam, message, jam_message_form_data(message), []
        )

    @post(
        "/guilds/{guild_id:str}/jams/{jam_id:uuid}/messages/{message_id:uuid}/edit",
        guards=_ADMIN,
    )
    async def jam_message_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        jam_id: UUID,
        message_id: UUID,
    ) -> Response:
        """Apply edits to an unsent message; a failed one is queued again."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error
        jam, message = await load_message(db_session, guild_id, jam_id, message_id)
        if message is None:
            return await render_not_found(request, db_session, guild_id, "Message")
        if message.status not in ("pending", "failed"):
            flash_error(request, "Only a message that has not been sent can be edited.")
            return Redirect(path=_jam_path(guild_id, jam_id))

        data = read_jam_message_form(await request.form())
        is_valid, errors, cleaned = validate_jam_message_form(data)
        if not is_valid:
            return await render_message_form(
                request, db_session, guild, jam, message, data, errors, 400
            )

        if not await store.save_message_edit(db_session, message, cleaned):
            flash_error(
                request,
                "This message changed while you were editing it. Nothing was saved.",
            )
            return Redirect(path=_jam_path(guild_id, jam_id))
        flash_success(request, "Message updated.")
        return Redirect(path=_jam_path(guild_id, jam_id))

    @post(
        "/guilds/{guild_id:str}/jams/{jam_id:uuid}/messages/{message_id:uuid}/retry",
        guards=_ADMIN,
    )
    async def jam_message_retry(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        jam_id: UUID,
        message_id: UUID,
    ) -> Response:
        """Queue a failed or stuck message to send now."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        _, message = await load_message(db_session, guild_id, jam_id, message_id)
        if message is None:
            flash_error(request, "Message not found.")
        elif await store.retry_message(db_session, message):
            flash_success(request, "Message queued. The bot sends it within a minute.")
        else:
            flash_error(request, "That message cannot be retried right now.")
        return Redirect(path=_jam_path(guild_id, jam_id))

    @post(
        "/guilds/{guild_id:str}/jams/{jam_id:uuid}/messages/{message_id:uuid}/delete",
        guards=_ADMIN,
    )
    async def jam_message_delete(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        jam_id: UUID,
        message_id: UUID,
    ) -> Response:
        """Delete a scheduled message."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        _, message = await load_message(db_session, guild_id, jam_id, message_id)
        if message is None:
            flash_error(request, "Message not found.")
        else:
            await db_session.delete(message)
            await db_session.commit()
            flash_success(request, "Message deleted.")
        return Redirect(path=_jam_path(guild_id, jam_id))

    @post(
        "/guilds/{guild_id:str}/jams/{jam_id:uuid}/entries/{thread_id:str}/remove",
        guards=_ADMIN,
    )
    async def jam_entry_remove(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        jam_id: UUID,
        thread_id: str,
    ) -> Response:
        """Stop counting a showcase post as an entry. The post stays in Discord."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is not None and await teams.remove_entry(db_session, jam, thread_id):
            flash_success(
                request, "Entry removed. The post itself is still in Discord."
            )
        else:
            flash_error(request, "Entry not found.")
        return Redirect(path=_jam_path(guild_id, jam_id))

    @post(
        "/guilds/{guild_id:str}/jams/{jam_id:uuid}/entries/{thread_id:str}/restore",
        guards=_ADMIN,
    )
    async def jam_entry_restore(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        jam_id: UUID,
        thread_id: str,
    ) -> Response:
        """Count a removed showcase post again."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        jam = await store.get_jam(db_session, guild_id, jam_id)
        refusal = (
            teams.NOT_FOUND
            if jam is None
            else await teams.restore_entry(db_session, jam, thread_id)
        )
        if refusal is None:
            flash_success(request, "Post restored. It counts again.")
        elif refusal == teams.ON_A_TEAM:
            flash_error(
                request,
                "Not restored: its author has joined a team since, and would "
                "be entered twice.",
            )
        else:
            flash_error(request, "Removed post not found.")
        return Redirect(path=_jam_path(guild_id, jam_id))

    @post("/guilds/{guild_id:str}/jams/{jam_id:uuid}/recount", guards=_ADMIN)
    async def jam_recount(
        self, request: Request, db_session: AsyncSession, guild_id: str, jam_id: UUID
    ) -> Response:
        """Drop the vote counts so the bot counts the reactions again."""
        if (
            refused := await csrf_refusal(request, _jam_path(guild_id, jam_id))
        ) is not None:
            return refused
        jam = await store.get_jam(db_session, guild_id, jam_id)
        if jam is None:
            flash_error(request, "Jam not found.")
            return Redirect(path=f"/admin/bot/guilds/{guild_id}/jams")
        await results.clear_votes(db_session, jam)
        flash_success(
            request,
            "The bot will count the votes again. Reload this page in a few minutes.",
        )
        return Redirect(path=_jam_path(guild_id, jam_id))


# -- Helpers ------------------------------------------------------------------

# (key, Discord name) for each channel a message can be scheduled into.
MESSAGE_CHANNELS = [(key, CHANNEL_NAMES[key]) for key in JAM_MESSAGE_CHANNEL_KEYS]


async def csrf_refusal(request: Request, back: str) -> Response | None:
    """Send a POST without a valid CSRF token back where it came from."""
    if await verify_csrf(request):
        return None
    flash_error(request, "Your session expired. Please try again.")
    return Redirect(path=back)


async def list_forums(guild_id: str) -> list[DiscordChannel]:
    """The guild's forum channels, or none when Discord cannot be asked."""
    try:
        channels = await get_admin_discord_client().get_guild_channels(guild_id)
    except DISCORD_ERRORS:
        return []
    return [channel for channel in channels if channel.type == _FORUM]


def discord_parts(setup: JamDiscordSetup, jam: Jam) -> list[dict]:
    """Every Discord piece the jam relies on, with its id once it exists."""
    return [
        {"label": f"{CATEGORY_NAME} category", "id": setup.category_id},
        *(
            {
                # The showcase can be a forum under any name.
                "label": "Showcase forum"
                if key == "showcase"
                else f"#{CHANNEL_NAMES[key]}",
                "id": setup.channel_id_for(key),
            }
            for key in JAM_CHANNEL_COLUMNS
        ),
        {"label": f"@{ROLE_NAME} role", "id": setup.role_id},
        {
            "label": f"“{jam.tag_name}” showcase tag",
            "id": jam.showcase_tag_id,
        },
        {
            "label": f"“{jam.year_tag_name}” showcase tag",
            "id": jam.year_tag_id,
        },
    ]


def apply_jam_edit(jam: Jam, cleaned: dict) -> None:
    """Write a valid form onto a jam.

    A renamed tag or a start in another year points the jam at a different
    forum tag. The old one stays in Discord untouched.
    """
    if cleaned["tag_name"].lower() != jam.tag_name.lower():
        jam.showcase_tag_id = None
    if str(cleaned["starts_at"].year) != jam.year_tag_name:
        jam.year_tag_id = None
    for field, value in cleaned.items():
        setattr(jam, field, value)


async def conflicts(
    db_session: AsyncSession, guild_id: str, cleaned: dict, jam: Jam | None
) -> list[str]:
    """Reasons a valid jam form clashes with the guild's other jams.

    Only the dates can clash. Two jams may share a showcase tag: a jam that
    comes back every year uses the same tag each time, and the year tag tells
    the entries apart.
    """
    errors: list[str] = []
    jam_id = jam.id if jam else None
    ends_at = (
        cleaned["winners_at"]
        or cleaned["voting_ends_at"]
        or cleaned["submissions_close_at"]
    )
    other = await store.overlapping_jam(
        db_session, guild_id, cleaned["starts_at"], ends_at, except_jam_id=jam_id
    )
    if other is not None:
        errors.append(
            f"These dates overlap “{other.name}” "
            f"({_utc_label(other.starts_at)} to {_utc_label(other.ends_at)}). "
            "A server runs one jam at a time"
        )
    return errors


def _utc_label(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


async def set_up_discord(db_session: AsyncSession, jam: Jam) -> JamSetupReport:
    """Run Discord setup for a jam and save whatever ids it produced."""
    try:
        client = get_admin_discord_client()
    except DiscordAdminError:
        report = JamSetupReport()
        report.errors.append("The Discord bot token is not configured.")
        return report
    # One setup run per guild at a time. A second run waits, then reads what
    # the first one made and finds nothing left to create.
    async with store.guild_lock(db_session, jam.guild_id):
        await db_session.refresh(jam)
        setup = await store.get_or_create_setup(db_session, jam.guild_id)
        try:
            report = await run_jam_setup(client, setup, jam)
        finally:
            # Ids recorded before a failure are kept, so the next run does
            # not create those pieces a second time.
            await db_session.commit()
    return report


def flash_setup_report(request: Request, report: JamSetupReport, done: str) -> None:
    """Tell the admin what setup did and what is left."""
    made = [*report.created, *(f"{label} (already there)" for label in report.adopted)]
    summary = done + (f" Set up: {', '.join(made)}." if made else "")
    if report.ok:
        flash_success(request, summary)
        return
    if made:
        flash_success(request, summary)
    flash_error(
        request,
        "Discord setup is not finished. "
        + " ".join(error.rstrip(".") + "." for error in report.errors)
        + " Fix the problem, then press Finish setup.",
    )


async def load_message(
    db_session: AsyncSession, guild_id: str, jam_id: UUID, message_id: UUID
) -> tuple[Jam | None, JamMessage | None]:
    """Load a message through its jam, so both must belong to the guild."""
    jam = await store.get_jam(db_session, guild_id, jam_id)
    if jam is None:
        return None, None
    return jam, await store.get_message(db_session, jam.id, message_id)


async def render(
    request: Request,
    db_session: AsyncSession,
    template: str,
    guild_id: str,
    status_code: int = 200,
    **context: Any,
) -> TemplateResponse:
    """Render a jam admin template with the shared admin context."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        template,
        context={
            "active_page": _ACTIVE_PAGE,
            "guild_id": guild_id,
            **context,
            **ctx,
        },
        status_code=status_code,
    )


async def render_not_found(
    request: Request, db_session: AsyncSession, guild_id: str, what: str
) -> TemplateResponse:
    """Render the shared guild error page for a missing jam or message."""
    return await render(
        request,
        db_session,
        "admin/bot/guilds/error.html",
        guild_id,
        404,
        error=f"{what} not found.",
        error_code=404,
    )


async def render_jam_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    jam: Jam | None,
    form_data: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the create form (``jam`` is None) or the edit form."""
    return await render(
        request,
        db_session,
        "admin/bot/jams/form.html",
        guild_id,
        status_code,
        guild=guild,
        jam=jam,
        form_data=form_data or read_jam_form({}),
        errors=errors,
    )


async def render_jam_detail(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    jam: Jam,
    message_form: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render a jam's page, optionally with a rejected new-message form."""
    messages = await store.list_messages(db_session, jam.id)
    # Plain values, read as each query returns: the entries query reloads the
    # team rows, and a template must never touch an attribute that needs a
    # database read.
    team_rows = [
        {
            "id": team.id,
            "name": team.name,
            "thread_id": team.thread_id,
            "member_ids": sorted(member.user_id for member in team.members),
        }
        for team in await teams.list_teams(db_session, jam.id)
    ]
    entry_rows = [
        {
            "thread_id": row.submission.thread_id,
            "author_id": row.submission.author_id,
            "team_id": row.team.id if row.team is not None else None,
            "team_name": row.team.name if row.team is not None else None,
            "extra_to": row.extra_to,
        }
        for row in await teams.list_entries(db_session, jam.id)
    ]
    removed_rows = [
        {
            "thread_id": post.thread_id,
            "author_id": post.author_id,
            "team_name": post.team.name if post.team is not None else None,
        }
        for post in await teams.list_removed(db_session, jam.id)
    ]
    counted_at = store.as_utc(jam.votes_counted_at) if jam.votes_counted_at else None
    result_rows = (
        None
        if counted_at is None
        else [
            {
                "rank": row.rank,
                "tied": row.tied,
                "thread_id": row.thread_id,
                "author_id": row.author_id,
                "team_name": row.team_name,
                "reactions": row.reactions,
                "reactors": row.reactors,
            }
            for row in await results.list_results(db_session, jam.id)
        ]
    )
    voting_ends_at = store.as_utc(jam.voting_ends_at) if jam.voting_ends_at else None
    now = datetime.now(UTC)
    setup = await store.get_setup(db_session, jam.guild_id) or JamDiscordSetup(
        guild_id=jam.guild_id
    )
    return await render(
        request,
        db_session,
        "admin/bot/jams/detail.html",
        jam.guild_id,
        status_code,
        guild=guild,
        jam=jam,
        discord_parts=discord_parts(setup, jam),
        setup_finished=not setup.missing_parts and not jam.missing_tags,
        teams=team_rows,
        entries=entry_rows,
        entry_team_ids={row["team_id"] for row in entry_rows if row["team_id"]},
        removed=removed_rows,
        results=result_rows,
        votes_counted_at=counted_at,
        voting_ended=voting_ends_at is not None and now > voting_ends_at,
        teams_closing=results.is_closing(jam, now),
        close_within_days=results.CLOSE_WITHIN.days,
        messages=messages,
        stuck_ids={message.id for message in messages if store.is_stuck(message)},
        message_channels=MESSAGE_CHANNELS,
        channel_names=CHANNEL_NAMES,
        role_name=ROLE_NAME,
        message_form=message_form or read_jam_message_form({}),
        message_content_max=MESSAGE_CONTENT_MAX,
        errors=errors,
        flash_messages=get_flash_messages(request),
    )


async def render_message_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    jam: Jam,
    message: JamMessage,
    form_data: dict,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the edit form for one scheduled message."""
    return await render(
        request,
        db_session,
        "admin/bot/jams/message_edit.html",
        jam.guild_id,
        status_code,
        guild=guild,
        jam=jam,
        message=message,
        form_data=form_data,
        errors=errors,
        message_channels=MESSAGE_CHANNELS,
        role_name=ROLE_NAME,
        message_content_max=MESSAGE_CONTENT_MAX,
    )
