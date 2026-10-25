"""Native Litestar port of the admin-handler bot API — the privileged tier.

Ports the legacy FastAPI ``routers/admin_handlers.py`` (prefix
``/admin/handlers``) — part of unit U9 in docs/v2/legacy-sunset/04-api-rewrite.md.
Separate from ``/handlers`` (member tier): created only via the admin slash
command (author-written script with admin powers), never editable by
members. Preserves the exact paths, verbs, status codes, and request/response
shapes so ``smarter_dev/bot/plugins/admin_handlers.py`` (and any external
caller) needs zero changes:

- ``POST   /api/admin/handlers`` → 201.
- ``PUT    /api/admin/handlers/{handler_id}`` → edit + reschedule.
- ``GET    /api/admin/handlers?guild_id=...`` → list (``include_scripts``).
- ``DELETE /api/admin/handlers/{handler_id}`` → 200 ``{"deleted": id}``.

Auth parity (SENSITIVE — verified against the legacy router): despite the
``/admin`` path prefix, the legacy routes required only a **valid API key**
(bare ``verify_api_key``, no admin scope check) — the Discord-side admin gate
lives in the bot plugin. Every route therefore takes :data:`BOT_API_GUARDS`
(``Permission("bot-api")``), NOT ``bot-api-admin``.

Error-shape parity: bare ``HTTPException`` plain ``{"detail": "<string>"}``
bodies via :func:`errors.plain_error`; malformed ``handler_id`` answers 422 via
:func:`errors.parse_uuid_path`.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from uuid import UUID

from litestar import Controller
from litestar import delete
from litestar import get
from litestar import post
from litestar import put
from litestar.status_codes import HTTP_200_OK
from litestar.status_codes import HTTP_201_CREATED
from pydantic import BaseModel
from pydantic import Field
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from skrift.workers import get_handle
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import parse_uuid_path
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.bump_channel_scope import BUMP_TRACKER_HANDLER
from smarter_dev.web.bump_channel_scope import CHANNELS_REFUSAL
from smarter_dev.web.bump_channel_scope import is_bump_tracker
from smarter_dev.web.handler_caps import MAX_ADMIN_HANDLERS_PER_GUILD
from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.handler_recurrence import RECURRING_CHAINS
from smarter_dev.web.handler_schedule import ScheduleError
from smarter_dev.web.handler_schedule import first_fire_at
from smarter_dev.web.handler_schedule import validate_time_trigger_settings
from smarter_dev.web.models import ADMIN_HANDLER_TRIGGER_TYPES
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.moderation_cutover import RETIRED_HANDLERS as COORDINATOR_REPLACED

logger = logging.getLogger(__name__)

_recurring_chain = RECURRING_CHAINS["admin"]

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]

_TIME_TRIGGERS = ("schedule", "timer")


class CreateAdminHandlerRequest(BaseModel):
    guild_id: str
    name: str
    trigger_type: str
    settings: dict = Field(default_factory=dict)
    channel_ids: list[str] = Field(default_factory=list)
    description: str
    script: str
    created_by_admin: str


class UpdateAdminHandlerRequest(BaseModel):
    description: str
    script: str
    settings: dict = Field(default_factory=dict)
    channel_ids: list[str] = Field(default_factory=list)
    # Optional rename; omitted = keep the current name.
    name: str | None = None


class ReplaceScriptRequest(BaseModel):
    """Change a handler's script and nothing else.

    ``expected_script`` is the script the caller read; the change is refused
    when the handler's script is no longer that, so two editors cannot
    overwrite each other unknowingly. What the full update touches —
    description, settings, scope, name, whether the handler is enabled — is
    left as it is.
    """

    script: str
    expected_script: str


class AdminHandlerResponse(BaseModel):
    handler_id: str
    guild_id: str
    name: str
    trigger_type: str
    channel_ids: list[str]
    settings: dict
    description: str
    enabled: bool


class AdminHandlerDetail(AdminHandlerResponse):
    script: str


def _to_response(record: AdminHandler) -> AdminHandlerResponse:
    return AdminHandlerResponse(
        handler_id=str(record.id),
        guild_id=record.guild_id,
        name=record.name,
        trigger_type=record.trigger_type,
        channel_ids=list(record.channel_ids or []),
        settings=record.settings or {},
        description=record.description,
        enabled=record.enabled,
    )


def _reject_bump_tracker_scope(record: AdminHandler, data) -> None:
    """The bump tracker's channels are the policy's (``bump_channel_scope``):
    an edit may not change its stored ``channel_ids`` or its name. Nor may
    another handler be renamed to the tracker's name, which would silently
    give it the policy's bump channels as its scope."""
    if not is_bump_tracker(record):
        if data.name is not None and _normalized_name(data.name) == BUMP_TRACKER_HANDLER:
            raise plain_error(
                422,
                f"{BUMP_TRACKER_HANDLER!r} is the Disboard bump tracker's name; "
                "another handler cannot be renamed to it",
            )
        return
    if list(data.channel_ids or []) != list(record.channel_ids or []):
        raise plain_error(422, CHANNELS_REFUSAL)
    if data.name is not None and _normalized_name(data.name) != record.name:
        raise plain_error(
            422, f"{record.name!r} is the Disboard bump tracker and cannot be renamed"
        )


def _reject_bot_optin_on_non_message(settings: dict, trigger_type: str) -> None:
    """422 when ``include_bot_messages`` is set on a non-message admin handler.

    The opt-in only means anything on a ``message`` trigger (it changes which
    bot/webhook messages fire the handler); admin handlers are the primary
    Disboard-confirmation consumer, so enforce the same rail as the member tier.
    """
    if settings.get("include_bot_messages") and trigger_type != "message":
        raise plain_error(
            422, "include_bot_messages is only valid on message-trigger handlers"
        )


def _normalized_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise plain_error(422, "name is required")
    if len(name) > 64:
        raise plain_error(422, "name is too long (max 64)")
    return name


async def _name_taken(
    session: AsyncSession, guild_id: str, name: str, exclude_id: UUID | None = None
) -> bool:
    query = select(AdminHandler.id).where(
        AdminHandler.guild_id == guild_id, AdminHandler.name == name
    )
    if exclude_id is not None:
        query = query.where(AdminHandler.id != exclude_id)
    return (await session.execute(query)).first() is not None


async def _reschedule(record: AdminHandler) -> None:
    """Cancel any pending fire and schedule the first fire from current settings."""
    validate_time_trigger_settings(
        record.trigger_type,
        record.settings or {},
        uses_agent="spawn_agent" in record.script,
    )
    if record.scheduled_job_id:
        try:
            await get_handle(record.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001 — best-effort; the chain also self-stops
            logger.warning("could not cancel job %s", record.scheduled_job_id)
        record.scheduled_job_id = None
    fire_at = first_fire_at(
        record.trigger_type, record.settings or {}, datetime.now(UTC)
    )
    await _recurring_chain.arm_occurrence(record, fire_at)


def _reject_removed_functions(script: str) -> None:
    """422 for a script naming a function the runtime no longer has: the
    sanctions (handler_moderation_policy)."""
    reason = moderation_refusal(script)
    if reason is not None:
        raise plain_error(422, reason)


class AdminHandlerController(Controller):
    """Guild-scoped privileged handlers: install, edit, list, delete."""

    path = "/api/admin/handlers"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("", status_code=HTTP_201_CREATED, guards=BOT_API_GUARDS)
    async def create_admin_handler(
        self,
        db_session: AsyncSession,
        data: CreateAdminHandlerRequest,
    ) -> AdminHandlerResponse:
        if data.trigger_type not in ADMIN_HANDLER_TRIGGER_TYPES:
            raise plain_error(422, "unknown trigger_type")
        _reject_bot_optin_on_non_message(data.settings, data.trigger_type)
        _reject_removed_functions(data.script)
        name = _normalized_name(data.name)
        if name == BUMP_TRACKER_HANDLER and data.channel_ids:
            raise plain_error(422, CHANNELS_REFUSAL)

        if await _name_taken(db_session, data.guild_id, name):
            raise plain_error(
                409,
                f"an admin handler named {name!r} already exists in this guild — edit it instead",
            )
        guild_count = (
            await db_session.execute(
                select(func.count())
                .select_from(AdminHandler)
                .where(AdminHandler.guild_id == data.guild_id)
            )
        ).scalar_one()
        if guild_count >= MAX_ADMIN_HANDLERS_PER_GUILD:
            raise plain_error(
                422,
                f"this guild already has {MAX_ADMIN_HANDLERS_PER_GUILD} admin handlers — "
                "delete or edit one instead",
            )

        record = AdminHandler(
            guild_id=data.guild_id,
            name=name,
            trigger_type=data.trigger_type,
            settings=data.settings,
            channel_ids=data.channel_ids,
            description=data.description,
            script=data.script,
            created_by_admin=data.created_by_admin,
        )
        db_session.add(record)
        await db_session.flush()

        if data.trigger_type in _TIME_TRIGGERS:
            try:
                await _reschedule(record)
            except ScheduleError as exc:
                raise plain_error(422, str(exc)) from exc

        await db_session.commit()
        await db_session.refresh(record)
        return _to_response(record)

    @put("/{handler_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def update_admin_handler(
        self,
        db_session: AsyncSession,
        handler_id: str,
        data: UpdateAdminHandlerRequest,
    ) -> AdminHandlerResponse:
        parsed_handler_id = parse_uuid_path(handler_id, "handler_id")
        # Locked until commit: the retired check below reads the live row,
        # and a concurrent write must not land between the check and this one
        # (the /script route's conditional UPDATE, done as a lock).
        record = await db_session.get(
            AdminHandler, parsed_handler_id, with_for_update=True
        )
        if record is None:
            raise plain_error(404, "admin handler not found")
        # trigger_type is immutable on edit, so validate against the stored one.
        _reject_bot_optin_on_non_message(data.settings, record.trigger_type)
        _reject_bump_tracker_scope(record, data)
        _reject_removed_functions(data.script)

        # Judged on the name the row had before this write: a switched-off
        # replaced handler renamed in the same save would otherwise come back.
        retired = not record.enabled and record.name in COORDINATOR_REPLACED
        if data.name is not None:
            name = _normalized_name(data.name)
            if retired and name != record.name:
                raise plain_error(
                    422,
                    f"{record.name!r} was replaced by the moderation coordinator "
                    "and cannot be renamed",
                )
            if await _name_taken(
                db_session, record.guild_id, name, exclude_id=record.id
            ):
                raise plain_error(
                    409,
                    f"an admin handler named {name!r} already exists in this guild",
                )
            record.name = name

        record.description = data.description
        record.script = data.script
        record.settings = data.settings
        record.channel_ids = data.channel_ids
        # Every write enables, except a handler the coordinator replaced
        # (scam-banner, edit-ping-catch) that was switched off: saving it must
        # not re-arm it next to the coordinator.
        if record.enabled or not retired:
            record.enabled = True

        if record.trigger_type in _TIME_TRIGGERS:
            try:
                await _reschedule(record)
            except ScheduleError as exc:
                raise plain_error(422, str(exc)) from exc

        await db_session.commit()
        await db_session.refresh(record)
        return _to_response(record)

    @put("/{handler_id:str}/script", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def replace_admin_handler_script(
        self,
        db_session: AsyncSession,
        handler_id: str,
        data: ReplaceScriptRequest,
    ) -> AdminHandlerResponse:
        """Put a reviewed script on a handler (``scripts/apply_handler_script.py``).

        A disabled handler stays disabled, unlike the full update, which
        enables on every write (a switched-off replaced handler aside).
        """
        parsed_handler_id = parse_uuid_path(handler_id, "handler_id")
        record = await db_session.get(AdminHandler, parsed_handler_id)
        if record is None:
            raise plain_error(404, "admin handler not found")
        _reject_removed_functions(data.script)
        # The write below only lands while the live script is still the one
        # the caller read. The check and the write are one statement, so two
        # callers who both read the old script cannot both get past it: the
        # row matches only
        # while the script is still what the caller read, and the one that
        # matched holds the row until commit. Comparing the loaded record
        # would let both through, each against its own stale copy.
        written = await db_session.execute(
            update(AdminHandler)
            .where(
                AdminHandler.id == parsed_handler_id,
                AdminHandler.script == data.expected_script,
            )
            .values(script=data.script)
            .execution_options(synchronize_session=False)
        )
        if written.rowcount != 1:
            raise plain_error(
                409, "the admin handler's script has changed since it was read"
            )
        # The row is ours until commit; read it again so the settings and
        # job id below are the ones in it now, not the ones loaded before
        # another writer's edit went through.
        await db_session.refresh(record)
        # A scheduled handler's settings are validated against its script.
        if record.trigger_type in _TIME_TRIGGERS:
            try:
                await _reschedule(record)
            except ScheduleError as exc:
                raise plain_error(422, str(exc)) from exc
        await db_session.commit()
        await db_session.refresh(record)
        return _to_response(record)

    @get("", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def list_admin_handlers(
        self,
        db_session: AsyncSession,
        guild_id: str,
        include_scripts: bool = False,
    ) -> list[AdminHandlerResponse] | list[AdminHandlerDetail]:
        """List a guild's admin handlers; ``include_scripts`` adds the script
        bodies (used by the admin author to decide edit-vs-create)."""
        rows = (
            (
                await db_session.execute(
                    select(AdminHandler).where(AdminHandler.guild_id == guild_id)
                )
            )
            .scalars()
            .all()
        )
        if include_scripts:
            return [
                AdminHandlerDetail(**_to_response(r).model_dump(), script=r.script)
                for r in rows
            ]
        return [_to_response(r) for r in rows]

    @delete("/{handler_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def delete_admin_handler(
        self,
        db_session: AsyncSession,
        handler_id: str,
    ) -> dict:
        parsed_handler_id = parse_uuid_path(handler_id, "handler_id")
        record = await db_session.get(AdminHandler, parsed_handler_id)
        if record is None:
            raise plain_error(404, "admin handler not found")
        if record.scheduled_job_id:
            try:
                await get_handle(record.scheduled_job_id).cancel()
            except Exception:  # noqa: BLE001 — best-effort; the chain also self-stops
                logger.warning("could not cancel job %s", record.scheduled_job_id)
        await db_session.delete(record)
        await db_session.commit()
        return {"deleted": str(parsed_handler_id)}
