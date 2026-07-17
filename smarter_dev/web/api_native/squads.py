"""Native Litestar port of the squad bot API (legacy ``routers/squads.py`` and
``routers/squad_sale_events.py``).

Preserves the exact paths, verbs, status codes, and request/response schemas of
the FastAPI implementation so ``smarter_dev/bot/services/squads_service.py`` (and
any external caller) needs zero changes. See docs/v2/legacy-sunset/04-api-rewrite.md
(unit U3 — the trailing-slash canary).

Error-shape parity notes:
- ``squads.py`` funnels every ``crud`` failure through the *secure* plain-body
  helpers (``security_utils.create_*``) — ``{"detail": "<string>"}`` — so the
  native port catches ``crud`` exceptions inline and re-raises them via
  :func:`errors.secure_*`, rather than relying on the flat crud-exception
  handlers used by the bytes controller.
- The one exception is ``get_user_squad``'s user-id check, which used the nested
  ``exceptions.validate_discord_id`` body — reproduced here via
  :func:`errors.validate_discord_id`.
- ``squad_sale_events.py`` wraps its whole body in ``except Exception`` blocks
  that answer 500 ``{"detail": "Internal server error"}``. Because a FastAPI
  ``HTTPException`` subclasses ``Exception``, the intended 404 for a missing
  event is swallowed into that 500 — a faithful reproduction is documented in
  :meth:`SquadSaleEventController.get_sale_event`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from litestar import Controller, Request, delete, get, post, put
from litestar.di import Provide
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.schemas import (
    ActiveSaleEventResponse,
    SquadCostInfo,
    SquadCreate,
    SquadJoinRequest,
    SquadLeaveRequest,
    SquadMembersResponse,
    SquadMembershipResponse,
    SquadResponse,
    SquadSaleEventResponse,
    SquadUpdate,
    SuccessResponse,
    UserSquadResponse,
)
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    plain_error,
    secure_database_error,
    secure_not_found_error,
    secure_validation_error,
    validate_discord_id,
)
from smarter_dev.web.crud import (
    ConflictError,
    DatabaseOperationError,
    NotFoundError,
    SquadOperations,
    SquadSaleEventOperations,
)
from smarter_dev.web.models import SquadMembership

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


async def provide_validated_guild_id(guild_id: str) -> str:
    """Validate the guild-id path param, matching the FastAPI 400 shape.

    Mirrors ``dependencies.verify_guild_access``: a bare ``{"detail": "Invalid
    guild ID"}`` with status 400.
    """
    try:
        if int(guild_id) <= 0:
            raise ValueError("Invalid guild ID")
    except ValueError:
        raise plain_error(400, "Invalid guild ID")
    return guild_id


def _parse_uuid_path(value: str, field_name: str) -> UUID:
    """Parse a UUID path segment, matching FastAPI's 422 on bad format.

    The FastAPI routes declared their id path params as ``UUID``, so a malformed
    UUID produced a 422 ``RequestValidationError``. Declaring the Litestar param
    as ``str`` and parsing here reproduces that 422 (via
    :func:`errors.handle_validation_exception`) instead of a route-miss 404.
    """
    try:
        return UUID(value)
    except ValueError as parse_error:
        raise ValidationException(
            detail=f"Invalid {field_name} format",
            extra=[{"key": field_name, "message": "value is not a valid uuid"}],
        ) from parse_error


def _cost_info(
    original_cost: int,
    current_cost: int,
    sale_event,
    is_switch: bool,
) -> SquadCostInfo:
    """Build a :class:`SquadCostInfo`, attaching an active sale when it discounts."""
    discount_percent = None
    active_sale = None
    if sale_event and current_cost < original_cost:
        discount_percent = (
            sale_event.switch_discount_percent
            if is_switch
            else sale_event.join_discount_percent
        )
        active_sale = ActiveSaleEventResponse(
            event_name=sale_event.name,
            event_id=sale_event.id,
            join_discount_percent=sale_event.join_discount_percent,
            switch_discount_percent=sale_event.switch_discount_percent,
            time_remaining_hours=sale_event.time_remaining_hours,
            end_time=sale_event.end_time,
        )
    return SquadCostInfo(
        original_cost=original_cost,
        current_cost=current_cost,
        discount_percent=discount_percent,
        active_sale=active_sale,
        is_on_sale=current_cost < original_cost,
    )


async def build_cost_info(
    switch_cost: int, guild_id: str, session: AsyncSession
) -> dict:
    """Return the ``join_cost_info``/``switch_cost_info`` entries for a squad.

    Pure with respect to its inputs — the caller merges the returned mapping into
    its own squad payload. Mirrors ``routers/squads._add_cost_info_to_squad``.
    """
    if switch_cost > 0:
        sale_ops = SquadSaleEventOperations(session)
        join_cost, join_event = await sale_ops.calculate_discounted_cost(
            guild_id=guild_id, original_cost=switch_cost, is_switch=False
        )
        switch_discounted_cost, switch_event = await sale_ops.calculate_discounted_cost(
            guild_id=guild_id, original_cost=switch_cost, is_switch=True
        )
        return {
            "join_cost_info": _cost_info(switch_cost, join_cost, join_event, is_switch=False),
            "switch_cost_info": _cost_info(
                switch_cost, switch_discounted_cost, switch_event, is_switch=True
            ),
        }

    no_cost = SquadCostInfo(
        original_cost=0,
        current_cost=0,
        discount_percent=None,
        active_sale=None,
        is_on_sale=False,
    )
    return {"join_cost_info": no_cost, "switch_cost_info": no_cost}


class SquadController(Controller):
    """Squad management — listing, creation, membership, and squad info."""

    path = "/api/guilds/{guild_id:str}/squads"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS
    dependencies = {"validated_guild_id": Provide(provide_validated_guild_id)}

    @get("/", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def list_squads(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        include_inactive: bool = False,
    ) -> list[SquadResponse]:
        """List all squads in a guild, with member counts and cost info."""
        squad_ops = SquadOperations()
        squads = await squad_ops.get_guild_squads(
            db_session, validated_guild_id, active_only=not include_inactive
        )

        squad_responses = []
        for squad in squads:
            member_count = await squad_ops._get_squad_member_count(db_session, squad.id)
            squad_data = squad.__dict__.copy()
            squad_data["member_count"] = member_count
            squad_data.update(
                await build_cost_info(squad_data["switch_cost"], validated_guild_id, db_session)
            )
            squad_responses.append(SquadResponse.model_validate(squad_data))
        return squad_responses

    @post("/", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def create_squad(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        data: SquadCreate,
    ) -> SquadResponse:
        """Create a new squad."""
        try:
            squad_ops = SquadOperations()
            created_squad = await squad_ops.create_squad(
                db_session,
                validated_guild_id,
                data.role_id,
                data.name,
                description=data.description,
                max_members=data.max_members,
                switch_cost=data.switch_cost,
                is_default=data.is_default,
            )
            await db_session.commit()
            await db_session.refresh(created_squad)

            squad_data = created_squad.__dict__.copy()
            squad_data["member_count"] = 0
            squad_data.update(
                await build_cost_info(squad_data["switch_cost"], validated_guild_id, db_session)
            )
            return SquadResponse.model_validate(squad_data)
        except ConflictError as conflict:
            raise secure_validation_error(str(conflict))
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)

    @get("/{squad_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_squad(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        squad_id: str,
    ) -> SquadResponse:
        """Get a squad by ID, scoped to the guild."""
        parsed_squad_id = _parse_uuid_path(squad_id, "squad_id")
        try:
            squad_ops = SquadOperations()
            squad = await squad_ops.get_squad(db_session, parsed_squad_id)

            if squad.guild_id != validated_guild_id:
                raise secure_not_found_error("Squad")

            member_count = await squad_ops._get_squad_member_count(db_session, parsed_squad_id)
            squad_data = squad.__dict__.copy()
            squad_data["member_count"] = member_count
            squad_data.update(
                await build_cost_info(squad_data["switch_cost"], validated_guild_id, db_session)
            )
            return SquadResponse.model_validate(squad_data)
        except NotFoundError:
            raise secure_not_found_error("Squad")
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)

    @put("/{squad_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def update_squad(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        squad_id: str,
        data: SquadUpdate,
    ) -> SquadResponse:
        """Update squad configuration; only provided fields change."""
        parsed_squad_id = _parse_uuid_path(squad_id, "squad_id")
        try:
            squad_ops = SquadOperations()
            squad = await squad_ops.get_squad(db_session, parsed_squad_id)

            if squad.guild_id != validated_guild_id:
                raise secure_not_found_error("Squad")

            update_data = data.model_dump(exclude_unset=True, exclude_none=True)
            if not update_data:
                raise secure_validation_error("No valid squad updates provided")

            for field_name, value in update_data.items():
                if hasattr(squad, field_name):
                    setattr(squad, field_name, value)

            await db_session.commit()

            member_count = await squad_ops._get_squad_member_count(db_session, parsed_squad_id)
            squad_data = squad.__dict__.copy()
            squad_data["member_count"] = member_count
            return SquadResponse.model_validate(squad_data)
        except NotFoundError:
            raise secure_not_found_error("Squad")
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)

    @post("/{squad_id:str}/join", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def join_squad(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        squad_id: str,
        data: SquadJoinRequest,
    ) -> SquadMembershipResponse:
        """Join a squad."""
        parsed_squad_id = _parse_uuid_path(squad_id, "squad_id")
        try:
            squad_ops = SquadOperations()
            membership = await squad_ops.join_squad(
                db_session, validated_guild_id, data.user_id, parsed_squad_id, data.username
            )
            await db_session.commit()

            squad = await squad_ops.get_squad(db_session, parsed_squad_id)
            member_count = await squad_ops._get_squad_member_count(db_session, parsed_squad_id)
            squad_data = squad.__dict__.copy()
            squad_data["member_count"] = member_count

            return SquadMembershipResponse(
                squad_id=membership.squad_id,
                user_id=membership.user_id,
                guild_id=membership.guild_id,
                joined_at=membership.joined_at,
                squad=SquadResponse.model_validate(squad_data),
            )
        except ConflictError as conflict:
            error_message = str(conflict)
            lowered = error_message.lower()
            if "already in squad" in lowered:
                raise plain_error(400, error_message)
            if "default squad" in lowered and "cannot manually join" in lowered:
                raise plain_error(400, error_message)
            raise secure_validation_error(error_message)
        except NotFoundError:
            raise secure_not_found_error("Squad")
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)

    @delete("/leave", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def leave_squad(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        data: SquadLeaveRequest,
    ) -> SuccessResponse:
        """Leave the caller's current squad."""
        try:
            squad_ops = SquadOperations()
            await squad_ops.leave_squad(db_session, validated_guild_id, data.user_id)
            await db_session.commit()
            return SuccessResponse(
                message=f"User {data.user_id} left their squad",
                timestamp=datetime.now(timezone.utc),
            )
        except NotFoundError:
            raise secure_not_found_error("Squad")
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)

    @get("/members/{user_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_user_squad(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        user_id: str,
    ) -> UserSquadResponse:
        """Get the user's current squad (or an empty result)."""
        try:
            validate_discord_id(request, user_id, "user ID")

            squad_ops = SquadOperations()
            squad = await squad_ops.get_user_squad(db_session, validated_guild_id, user_id)

            if squad is None:
                return UserSquadResponse(
                    user_id=user_id,
                    guild_id=validated_guild_id,
                    squad=None,
                    membership=None,
                )

            membership_stmt = select(SquadMembership).where(
                SquadMembership.guild_id == validated_guild_id,
                SquadMembership.user_id == user_id,
                SquadMembership.squad_id == squad.id,
            )
            result = await db_session.execute(membership_stmt)
            membership = result.scalar_one()

            member_count = await squad_ops._get_squad_member_count(db_session, squad.id)
            squad_data = squad.__dict__.copy()
            squad_data["member_count"] = member_count
            squad_response = SquadResponse.model_validate(squad_data)

            return UserSquadResponse(
                user_id=user_id,
                guild_id=validated_guild_id,
                squad=squad_response,
                membership=SquadMembershipResponse(
                    squad_id=membership.squad_id,
                    user_id=membership.user_id,
                    guild_id=membership.guild_id,
                    joined_at=membership.joined_at,
                    squad=squad_response,
                ),
            )
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)

    @get("/{squad_id:str}/members", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_squad_members(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        squad_id: str,
    ) -> SquadMembersResponse:
        """List all members of a squad."""
        parsed_squad_id = _parse_uuid_path(squad_id, "squad_id")
        try:
            squad_ops = SquadOperations()
            squad = await squad_ops.get_squad(db_session, parsed_squad_id)

            if squad.guild_id != validated_guild_id:
                raise secure_not_found_error("Squad")

            memberships = await squad_ops.get_squad_members(db_session, parsed_squad_id)
            member_count = len(memberships)

            squad_data = squad.__dict__.copy()
            squad_data["member_count"] = member_count
            squad_response = SquadResponse.model_validate(squad_data)

            member_responses = [
                SquadMembershipResponse(
                    squad_id=membership.squad_id,
                    user_id=membership.user_id,
                    guild_id=membership.guild_id,
                    joined_at=membership.joined_at,
                    squad=squad_response,
                )
                for membership in memberships
            ]

            return SquadMembersResponse(
                squad=squad_response,
                members=member_responses,
                total_members=member_count,
            )
        except NotFoundError:
            raise secure_not_found_error("Squad")
        except DatabaseOperationError as db_error:
            raise secure_database_error(db_error)


def _sale_event_response(sale_event) -> SquadSaleEventResponse:
    """Serialize a sale event, attaching its computed time-window fields.

    Mirrors ``routers/squad_sale_events``: ``model_validate`` then explicit
    assignment of the model's computed properties.
    """
    response = SquadSaleEventResponse.model_validate(sale_event)
    response.end_time = sale_event.end_time
    response.is_currently_active = sale_event.is_currently_active
    response.has_started = sale_event.has_started
    response.has_ended = sale_event.has_ended
    response.time_remaining_hours = sale_event.time_remaining_hours
    response.days_until_start = sale_event.days_until_start
    return response


class SquadSaleEventController(Controller):
    """Squad sale event lookups — read-only listing and detail."""

    path = "/api/guilds/{guild_id:str}/squad-sale-events"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS
    dependencies = {"validated_guild_id": Provide(provide_validated_guild_id)}

    @get("/", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def list_sale_events(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
    ) -> list[SquadSaleEventResponse]:
        """List all sale events for a guild.

        The legacy router answers any error with a 500 ``{"detail": "Internal
        server error"}`` — reproduced here.
        """
        try:
            sale_ops = SquadSaleEventOperations(db_session)
            events, _ = await sale_ops.get_sale_events_by_guild(validated_guild_id)
            return [_sale_event_response(event) for event in events]
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{event_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_sale_event(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        event_id: str,
    ) -> SquadSaleEventResponse:
        """Get a specific sale event by ID.

        A malformed event UUID answers 422 (the FastAPI ``UUID`` path param
        validated before the handler ran). Faithful port of the rest of the
        legacy behavior: the router's ``except Exception`` block also catches the
        ``HTTPException`` its own not-found branch raises (a FastAPI
        ``HTTPException`` subclasses ``Exception``), so a valid-but-missing event
        — like any other error — answers 500 ``{"detail": "Internal server
        error"}`` rather than 404.
        """
        parsed_event_id = _parse_uuid_path(event_id, "event_id")
        try:
            sale_ops = SquadSaleEventOperations(db_session)
            event = await sale_ops.get_sale_event_by_id(parsed_event_id, validated_guild_id)
            if not event:
                raise NotFoundError("Sale event not found")
            return _sale_event_response(event)
        except Exception:
            raise plain_error(500, "Internal server error")
