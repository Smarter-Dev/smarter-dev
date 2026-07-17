"""Native Litestar port of the daily-quest bot API (legacy ``routers/quests.py``).

Preserves the exact paths, verbs, status codes, and request/response shapes of
the FastAPI implementation so ``smarter_dev/bot`` (and any external caller) needs
zero changes. See docs/v2/legacy-sunset/04-api-rewrite.md (unit U5).

NOT registered in ``app.yaml`` yet — the FastAPI mount still owns ``/api``. This
module exists for isolated parity tests until the atomic switchover.

Session note: the legacy router split work across a Skrift-schema session
(quest tables) and a legacy session (squad/bytes tables). Post phase-02 the two
collapse into the single injected ``db_session``; the ops that took a separate
``legacy_session`` are handed the same session (``QuestSubmissionOperations``
defaults ``legacy_session`` to its primary session).

Error-shape parity: the legacy router answered every failure with a bare
``HTTPException`` — a plain ``{"detail": "<string>"}`` body — so this port raises
:func:`errors.plain_error` with the identical status codes and detail strings. A
malformed ``daily_quest_id`` answers 422 (the FastAPI ``UUID`` path param
validated before the handler ran), reproduced via :func:`_parse_uuid_path`.

Rate-limiting parity is deferred to the switchover commit (see the bytes module
and the plan's "Rate-limiting parity" section); the FastAPI mount still enforces
those windows in production until switchover.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from litestar import Controller, get, post
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard

from smarter_dev.shared.date_provider import get_date_provider
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    plain_error,
)
from smarter_dev.web.crud import (
    DatabaseOperationError,
    QuestInputOperations,
    QuestOperations,
    QuestSubmissionOperations,
    ScriptExecutionError,
    SquadOperations,
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


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


class DailyQuestSubmitBody(BaseModel):
    """Request body for submitting a daily-quest solution."""

    guild_id: str
    user_id: str
    submitted_solution: str


class QuestController(Controller):
    """Daily-quest endpoints — current quest, input, submission, scoreboards."""

    path = "/api/quests"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/daily/current", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_current_daily_quest(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> dict[str, Any]:
        """Return the guild's active daily quest, or a null-quest message."""
        today = get_date_provider().today()

        try:
            quest_ops = QuestOperations(db_session)
            daily = await quest_ops.get_daily_quest(active_date=today, guild_id=guild_id)

            if not daily or not daily.quest or not daily.is_active:
                return {"quest": None, "message": "No daily quest available yet"}

            quest_data = {
                "id": str(daily.id),
                "title": daily.quest.title,
                "prompt": daily.quest.prompt,
                "quest_type": daily.quest.quest_type,
                "active_date": daily.active_date.isoformat(),
                "expires_at": daily.expires_at.isoformat(),
                "hint": "Once you're ready, submit with /daily submit",
            }
            return {"quest": quest_data}
        except DatabaseOperationError as db_error:
            if "UndefinedTableError" in str(db_error):
                return {"quest": None, "message": "Quest tables not yet created"}
            raise plain_error(500, "Failed to retrieve daily quest")

    @post("/{daily_quest_id:str}/submit", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def submit_daily_quest(
        self,
        db_session: AsyncSession,
        daily_quest_id: str,
        data: DailyQuestSubmitBody,
    ) -> dict[str, Any]:
        """Submit a solution for the daily quest and score it for the squad."""
        parsed_quest_id = _parse_uuid_path(daily_quest_id, "daily_quest_id")
        guild_id = data.guild_id
        user_id = data.user_id
        submitted_solution = data.submitted_solution

        squad_ops = SquadOperations()
        user_squad = await squad_ops.get_user_squad(db_session, guild_id, user_id)
        if not user_squad:
            raise plain_error(404, "User is not a member of any squad")

        quest_ops = QuestOperations(db_session)
        daily_quest = await quest_ops.get_daily_quest_by_id(parsed_quest_id, guild_id)
        if not daily_quest:
            raise plain_error(404, "Daily quest not found")
        if not daily_quest.is_active:
            raise plain_error(403, "Daily quest is not active")

        submission_ops = QuestSubmissionOperations(db_session)
        is_correct, is_first_success, points = await submission_ops.submit_solution(
            daily_quest_id=parsed_quest_id,
            guild_id=guild_id,
            squad_id=user_squad.id,
            user_id=user_id,
            username=user_id,
            submitted_solution=submitted_solution,
        )

        return {
            "is_correct": is_correct,
            "is_first_success": is_first_success,
            "points_earned": points,
        }

    @get("/{daily_quest_id:str}/input", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_daily_quest_input(
        self,
        db_session: AsyncSession,
        daily_quest_id: str,
        guild_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Return (generating if needed) the shared input for a daily quest."""
        parsed_quest_id = _parse_uuid_path(daily_quest_id, "daily_quest_id")
        try:
            squad_ops = SquadOperations()
            user_squad = await squad_ops.get_user_squad(db_session, guild_id, user_id)
            if not user_squad:
                raise plain_error(404, "User is not a member of any squad")

            quest_ops = QuestOperations(db_session)
            daily_quest = await quest_ops.get_daily_quest_by_id(parsed_quest_id, guild_id)
            if not daily_quest:
                raise plain_error(404, "Daily quest not found")

            if daily_quest.guild_id != guild_id:
                raise plain_error(403, "Quest does not belong to the specified guild")

            if not daily_quest.is_active:
                raise plain_error(403, "This quest is no longer / not yet active")

            quest = daily_quest.quest
            input_ops = QuestInputOperations(db_session)

            if not quest.input_generator_script:
                input_data = "No input required for this quest."
            else:
                try:
                    input_data, _ = await input_ops.get_or_create_input(
                        daily_quest_id=daily_quest.id,
                        script=quest.input_generator_script,
                    )
                except ScriptExecutionError:
                    raise plain_error(500, "Failed to generate quest input")

            return {
                "input_data": input_data,
                "quest": {
                    "id": str(quest.id),
                    "title": quest.title,
                    "prompt": quest.prompt,
                    "type": quest.quest_type,
                },
                "daily_quest": {
                    "id": str(daily_quest.id),
                    "active_date": str(daily_quest.active_date),
                    "expires_at": daily_quest.expires_at.isoformat(),
                },
                "squad": {
                    "id": str(user_squad.id),
                    "name": user_squad.name,
                },
                "metadata": {
                    "has_existing_input": True,
                },
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve quest input")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/scoreboard", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_daily_quest_scoreboard(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> dict[str, Any]:
        """Return the squad scoreboard for today's daily quest."""
        today = get_date_provider().today()

        try:
            quest_ops = QuestOperations(db_session)
            daily = await quest_ops.get_daily_quest(active_date=today, guild_id=guild_id)

            if not daily:
                return {"quest": None, "scoreboard": []}

            submission_ops = QuestSubmissionOperations(db_session)
            scoreboard = await submission_ops.get_daily_quest_scoreboard(daily.id)

            return {
                "quest": {
                    "id": str(daily.id),
                    "title": daily.quest.title,
                    "active_date": daily.active_date.isoformat(),
                    "expires_at": daily.expires_at.isoformat(),
                },
                "scoreboard": scoreboard,
            }
        except DatabaseOperationError as db_error:
            if "UndefinedTableError" in str(db_error):
                return {"quest": None, "scoreboard": []}
            raise plain_error(500, "Failed to retrieve daily quest scoreboard")

    @get("/detailed-scoreboard", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_detailed_scoreboard(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> dict[str, Any] | None:
        """Return the detailed daily-quest scoreboard.

        Faithful port of the legacy handler, including its behavior of returning
        ``null`` (no explicit ``return``) once a daily quest exists — the legacy
        implementation never built a body for that branch.
        """
        try:
            quest_ops = QuestOperations(db_session)
            today = get_date_provider().today()
            daily = await quest_ops.get_daily_quest(today, guild_id)

            if not daily:
                return {
                    "quest": None,
                    "detailed_scoreboard": [],
                    "total_submissions": 0,
                    "total_challenges": 0,
                }
            return None
        except DatabaseOperationError as db_error:
            if "UndefinedTableError" in str(db_error):
                return {
                    "quest": None,
                    "detailed_scoreboard": [],
                    "total_submissions": 0,
                    "total_challenges": 0,
                }
            raise plain_error(500, "Failed to retrieve detailed scoreboard data")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/upcoming-announcements", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_upcoming_quest_announcements(
        self,
        db_session: AsyncSession,
        seconds: int = 45,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return daily quests becoming active within the next ``seconds``."""
        try:
            quest_ops = QuestOperations(db_session)
            now = datetime.now(timezone.utc)
            window_end = now + timedelta(seconds=seconds)

            quests = await quest_ops.get_upcoming_daily_quests(window_end=window_end)

            quest_list = [
                {
                    "id": str(dq.id),
                    "quest_id": str(dq.quest.id),
                    "title": dq.quest.title,
                    "description": dq.quest.prompt,
                    "guild_id": dq.guild_id,
                    "release_time": dq.active_date.isoformat(),
                }
                for dq in quests
            ]
            return {"quests": quest_list}
        except DatabaseOperationError as db_error:
            if "UndefinedTableError" in str(db_error):
                return {"quests": []}
            raise plain_error(500, "Failed to retrieve upcoming quests")

    @post(
        "/{daily_quest_id:str}/mark-announced",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def mark_daily_quest_announced(
        self,
        db_session: AsyncSession,
        daily_quest_id: str,
    ) -> dict[str, bool]:
        """Mark a daily quest as announced."""
        parsed_quest_id = _parse_uuid_path(daily_quest_id, "daily_quest_id")
        try:
            quest_ops = QuestOperations(db_session)
            success = await quest_ops.mark_daily_quest_announced(parsed_quest_id)
            if not success:
                raise plain_error(404, "Daily quest not found")
            return {"success": True}
        except DatabaseOperationError as db_error:
            if "UndefinedTableError" in str(db_error):
                raise plain_error(404, "Quest tables not yet created")
            raise plain_error(500, "Failed to mark quest announced")

    @post(
        "/{daily_quest_id:str}/mark-active",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def mark_daily_quest_active(
        self,
        db_session: AsyncSession,
        daily_quest_id: str,
    ) -> dict[str, bool]:
        """Mark a daily quest as active."""
        parsed_quest_id = _parse_uuid_path(daily_quest_id, "daily_quest_id")
        try:
            quest_ops = QuestOperations(db_session)
            success = await quest_ops.mark_daily_quest_active(parsed_quest_id)
            if not success:
                raise plain_error(404, "Daily quest not found")
            return {"success": True}
        except DatabaseOperationError as db_error:
            if "UndefinedTableError" in str(db_error):
                raise plain_error(404, "Quest tables not yet created")
            raise plain_error(500, "Failed to activate daily quest")
