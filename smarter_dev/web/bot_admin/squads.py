"""Squad configuration and squad sale events for the Skrift admin panel.

Ports ``squads_config`` and the ``squad_sale_event_*`` views from the legacy
``smarter_dev.web.admin.views`` onto a Skrift-native Litestar controller under
``/admin/bot``. Guild identity comes from Discord (via
:mod:`smarter_dev.web.discord_admin_client`); squads and sale events live in the
``squads`` / ``squad_sale_events`` tables.

Form parsing is factored into pure module-level helpers so the accepted-field
contract can be unit-tested without a request or a database.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.crud import (
    ConflictError,
    SquadOperations,
    SquadSaleEventOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    GuildNotFoundError,
    get_admin_discord_client,
)

logger = logging.getLogger(__name__)

squad_ops = SquadOperations()


def parse_datetime_local(value: str) -> datetime:
    """Parse a ``datetime-local`` form value into a naive datetime.

    Mirrors the legacy view, which accepts the HTML ``datetime-local`` format
    (``YYYY-MM-DDTHH:MM``) by swapping the ``T`` separator for a space before
    handing it to :meth:`datetime.fromisoformat`.

    Raises:
        ValueError: If ``value`` is empty or not an ISO datetime.
        AttributeError: If ``value`` is ``None``.
    """
    return datetime.fromisoformat(value.replace("T", " "))


def parse_squad_create_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted create-squad form into keyword args for the model.

    Pure function — no I/O. Mirrors the legacy view's accepted fields: blank
    optional text fields collapse to ``None``, ``max_members`` is optional, and
    the ``is_default`` checkbox is truthy only when set to ``"on"``.

    Raises:
        ValueError: If ``switch_cost`` or ``max_members`` is not an integer.
    """
    return {
        "role_id": form.get("role_id"),
        "name": form.get("name"),
        "description": form.get("description") or None,
        "welcome_message": form.get("welcome_message") or None,
        "announcement_channel": form.get("announcement_channel") or None,
        "switch_cost": int(form.get("switch_cost", 50)),
        "max_members": int(form["max_members"]) if form.get("max_members") else None,
        "is_default": form.get("is_default") == "on",
    }


def parse_squad_update_form(form: Mapping[str, str]) -> tuple[UUID, dict]:
    """Parse an update-squad form into ``(squad_id, updates)``.

    Pure function — no I/O. Same field rules as the create form plus the
    ``is_active`` checkbox.

    Raises:
        ValueError: If ``squad_id`` is not a UUID or a numeric field is invalid.
    """
    squad_id = UUID(form.get("squad_id"))
    updates = {
        "name": form.get("name"),
        "description": form.get("description") or None,
        "welcome_message": form.get("welcome_message") or None,
        "announcement_channel": form.get("announcement_channel") or None,
        "switch_cost": int(form.get("switch_cost")),
        "max_members": int(form["max_members"]) if form.get("max_members") else None,
        "is_active": form.get("is_active") == "on",
        "is_default": form.get("is_default") == "on",
    }
    return squad_id, updates


def parse_sale_event_form(form: Mapping[str, str]) -> dict:
    """Parse a create-sale-event form into keyword args for the model.

    Pure function — no I/O. Discount percentages default to ``0`` when absent.

    Raises:
        ValueError: If the start time, duration, or a discount is invalid.
        AttributeError: If ``start_time`` is missing.
    """
    return {
        "name": form.get("name"),
        "description": form.get("description") or "",
        "start_time": parse_datetime_local(form.get("start_time")),
        "duration_hours": int(form.get("duration_hours")),
        "join_discount_percent": int(form.get("join_discount_percent", 0)),
        "switch_discount_percent": int(form.get("switch_discount_percent", 0)),
    }


def parse_sale_event_update_form(form: Mapping[str, str]) -> dict:
    """Parse an edit-sale-event form, adding the ``is_active`` toggle.

    The edit form submits ``is_active`` as ``"true"`` for a checked switch.

    Raises:
        ValueError / AttributeError: As :func:`parse_sale_event_form`.
    """
    updates = parse_sale_event_form(form)
    updates["is_active"] = form.get("is_active") == "true"
    return updates


async def fetch_guild_or_error(
    request: Request,
    db_session: AsyncSession,
    guild_id: str,
    active_page: str,
) -> tuple[object | None, TemplateResponse | None]:
    """Fetch guild details, returning an error template on failure.

    Returns ``(guild, None)`` on success or ``(None, response)`` when the guild
    is missing (404) or Discord is unavailable (503). Mirrors the error
    handling used by the sibling bytes-config controller.
    """
    client = get_admin_discord_client()
    try:
        guild = await client.get_guild(guild_id)
    except GuildNotFoundError:
        ctx = await get_admin_context(request, db_session)
        return None, TemplateResponse(
            "admin/bot/guilds/error.html",
            context={
                "error": f"Guild {guild_id} not found or bot is not a member.",
                "error_code": 404,
                "active_page": active_page,
                "guild_id": guild_id,
                **ctx,
            },
            status_code=404,
        )
    except DiscordAdminError as exc:
        ctx = await get_admin_context(request, db_session)
        return None, TemplateResponse(
            "admin/bot/guilds/error.html",
            context={
                "error": f"Discord API error: {exc}",
                "error_code": 503,
                "active_page": active_page,
                "guild_id": guild_id,
                **ctx,
            },
            status_code=503,
        )
    return guild, None


async def apply_squad_action(
    db_session: AsyncSession,
    guild_id: str,
    action: str | None,
    form: Mapping[str, str],
) -> None:
    """Dispatch a squad create/update/delete action (no commit).

    Raises:
        ValueError: For an unknown action or invalid numeric/UUID input.
        ConflictError / IntegrityError: On a squad or default-squad conflict.
    """
    if action == "create":
        await squad_ops.create_squad(
            db_session, guild_id=guild_id, **parse_squad_create_form(form)
        )
    elif action == "update":
        squad_id, updates = parse_squad_update_form(form)
        await squad_ops.update_squad(db_session, squad_id, updates)
    elif action == "delete":
        await squad_ops.delete_squad(db_session, UUID(form.get("squad_id")))
    else:
        raise ValueError(f"Unknown squad action: {action!r}")


class SquadsAdminController(Controller):
    """Squad config and squad sale events under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- Squads config --------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/squads",
        guards=[auth_guard, Permission("administrator")],
    )
    async def squads_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the squad management page for one guild."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "squads"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        client = get_admin_discord_client()
        guild_roles = await client.get_guild_roles(guild_id)
        try:
            channels = await client.get_announcement_channels(guild_id)
        except DiscordAdminError:
            logger.warning(
                "Failed to fetch channels for guild %s; using empty list", guild_id
            )
            channels = []

        squads = await squad_ops.get_guild_squads(db_session, guild_id)
        squad_members = await squad_ops.get_all_guild_squad_members(
            db_session, guild_id
        )

        return TemplateResponse(
            "admin/bot/squads/config.html",
            context={
                "guild": guild,
                "guild_roles": guild_roles,
                "channels": channels,
                "squads": squads,
                "squad_members": squad_members,
                "active_page": "squads",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/squads",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_squads_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Create, update, or delete a squad, then redirect back."""
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/squads")

        form = await request.form()
        action = form.get("action")

        try:
            await apply_squad_action(db_session, guild_id, action, form)
        except (ValueError, TypeError):
            await db_session.rollback()
            flash_error(
                request, "Invalid squad configuration. Please check your input."
            )
            return redirect
        except (ConflictError, IntegrityError) as exc:
            await db_session.rollback()
            flash_error(request, _squad_conflict_message(exc))
            return redirect

        await db_session.commit()
        flash_success(request, _squad_success_message(action))
        return redirect

    # -- Squad sale events ----------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/squad-sale-events",
        guards=[auth_guard, Permission("administrator")],
    )
    async def sale_events_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """List every sale event for a guild plus the currently-active ones."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "sale_events"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        sale_ops = SquadSaleEventOperations(db_session)
        events, _ = await sale_ops.get_sale_events_by_guild(guild_id)
        active_events = await sale_ops.get_active_sale_events(guild_id)

        return TemplateResponse(
            "admin/bot/squads/sale_events.html",
            context={
                "guild": guild,
                "events": events,
                "active_events": active_events,
                "active_page": "sale_events",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/squad-sale-events",
        guards=[auth_guard, Permission("administrator")],
    )
    async def create_sale_event(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Create a new sale event, then redirect back to the list."""
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/squad-sale-events")

        form = await request.form()
        sale_ops = SquadSaleEventOperations(db_session)
        try:
            event_data = parse_sale_event_form(form)
        except (ValueError, TypeError, AttributeError):
            flash_error(request, "Invalid sale event. Please check your input.")
            return redirect

        try:
            await sale_ops.create_sale_event(
                guild_id=guild_id, created_by="admin", **event_data
            )
        except ConflictError as exc:
            flash_error(request, str(exc))
            return redirect

        flash_success(request, "Sale event created successfully!")
        return redirect

    @post(
        "/guilds/{guild_id:str}/squad-sale-events/{event_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def edit_sale_event(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        event_id: UUID,
    ) -> Redirect:
        """Update an existing sale event, then redirect back to the list."""
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/squad-sale-events")

        form = await request.form()
        sale_ops = SquadSaleEventOperations(db_session)
        try:
            updates = parse_sale_event_update_form(form)
        except (ValueError, TypeError, AttributeError):
            flash_error(request, "Invalid sale event. Please check your input.")
            return redirect

        try:
            updated = await sale_ops.update_sale_event(event_id, guild_id, **updates)
        except ConflictError as exc:
            flash_error(request, str(exc))
            return redirect

        if updated is None:
            flash_error(request, "Sale event not found.")
        else:
            flash_success(request, "Sale event updated successfully!")
        return redirect

    @post(
        "/guilds/{guild_id:str}/squad-sale-events/{event_id:uuid}/toggle",
        guards=[auth_guard, Permission("administrator")],
    )
    async def toggle_sale_event(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        event_id: UUID,
    ) -> Redirect:
        """Flip a sale event's active status, then redirect back to the list."""
        sale_ops = SquadSaleEventOperations(db_session)
        toggled = await sale_ops.toggle_sale_event(event_id, guild_id)

        if toggled is None:
            flash_error(request, "Sale event not found.")
        else:
            state = "enabled" if toggled.is_active else "disabled"
            flash_success(request, f"Sale event {state}.")
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/squad-sale-events")

    @post(
        "/guilds/{guild_id:str}/squad-sale-events/{event_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def delete_sale_event(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        event_id: UUID,
    ) -> Redirect:
        """Delete a sale event, then redirect back to the list."""
        sale_ops = SquadSaleEventOperations(db_session)
        deleted = await sale_ops.delete_sale_event(event_id, guild_id)

        if deleted:
            flash_success(request, "Sale event deleted successfully!")
        else:
            flash_error(request, "Sale event not found.")
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/squad-sale-events")


def _squad_success_message(action: str | None) -> str:
    """Human-readable success flash for a completed squad action."""
    return {
        "create": "Squad created successfully!",
        "update": "Squad updated successfully!",
        "delete": "Squad deleted successfully!",
    }.get(action, "Squad configuration updated.")


def _squad_conflict_message(exc: Exception) -> str:
    """Map a squad conflict/integrity error to a friendly flash message.

    Mirrors the legacy view's branching on the default-squad unique constraint
    versus a role that is already assigned to another squad.
    """
    error_str = str(exc)
    if isinstance(exc, ConflictError) and "default squad" in error_str.lower():
        return f"Default squad conflict: {error_str}"
    if "uq_squads_guild_default" in error_str:
        return "Squad configuration conflict. Only one default squad is allowed per guild."
    return (
        "Squad configuration conflict. The role may already be assigned "
        "to another squad."
    )
