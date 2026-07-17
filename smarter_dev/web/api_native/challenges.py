"""Native Litestar port of the challenge bot API (legacy ``routers/challenges.py``).

Provides announcement management, release scheduling, scoreboards, input
generation, and solution submission for campaign challenges. Preserves the exact
paths, verbs, status codes, and request/response shapes of the FastAPI
implementation so ``smarter_dev/bot`` (and any external caller) needs zero
changes. See docs/v2/legacy-sunset/04-api-rewrite.md (unit U4).

Route-order parity: the legacy router declared the static segments
(``/scoreboard``, ``/upcoming-campaign``, ``/detailed-scoreboard``,
``/upcoming-announcements``, ``/pending-announcements``) alongside the
``/{challenge_id}`` catch-all. Litestar always prefers literal path components
over path params, so the static routes win without any manual ordering — asserted
by the route-order tests.

Error-shape parity: the legacy router answered every failure with a bare
``HTTPException`` — a plain ``{"detail": "<string>"}`` body — reproduced here via
:func:`errors.plain_error` with identical status codes and detail strings. A
malformed ``challenge_id`` answers 422 (the FastAPI ``UUID`` path param validated
before the handler ran), reproduced via :func:`_parse_uuid_path`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from litestar import Controller, get, post
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    plain_error,
)
from smarter_dev.web.crud import (
    CampaignOperations,
    ChallengeInputOperations,
    ChallengeSubmissionOperations,
    DatabaseOperationError,
    ScriptExecutionError,
    SquadOperations,
)
from smarter_dev.web.models import Campaign

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


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


class SolutionSubmissionRequest(BaseModel):
    """Request body for submitting a challenge solution."""

    guild_id: str
    user_id: str
    username: str
    submitted_solution: str


class ChallengeController(Controller):
    """Challenge endpoints — announcements, releases, scoreboards, submissions."""

    path = "/api/challenges"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/upcoming-announcements", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_upcoming_announcements(
        self,
        db_session: AsyncSession,
        seconds: int = 45,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return challenges that will be announced within the next ``seconds``."""
        try:
            campaign_ops = CampaignOperations(db_session)
            upcoming_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            challenges = await campaign_ops.get_upcoming_announcements(upcoming_time)

            challenge_list = []
            for challenge in challenges:
                campaign = challenge.campaign
                release_time = campaign.start_time + timedelta(
                    hours=campaign.release_cadence_hours * (challenge.order_position - 1)
                )
                challenge_list.append(
                    {
                        "id": str(challenge.id),
                        "title": challenge.title,
                        "description": challenge.description,
                        "guild_id": campaign.guild_id,
                        "announcement_channels": campaign.announcement_channels,
                        "order_position": challenge.order_position,
                        "release_time": release_time.isoformat(),
                        "campaign": {
                            "id": str(campaign.id),
                            "title": campaign.title,
                            "start_time": campaign.start_time.isoformat(),
                            "release_cadence_hours": campaign.release_cadence_hours,
                        },
                    }
                )
            return {"challenges": challenge_list}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve upcoming announcements")
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/pending-announcements", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_pending_announcements(
        self,
        db_session: AsyncSession,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return challenges that should be announced but have not been yet."""
        try:
            campaign_ops = CampaignOperations(db_session)
            challenges = await campaign_ops.get_pending_announcements()

            challenge_list = []
            for challenge in challenges:
                campaign = challenge.campaign
                challenge_list.append(
                    {
                        "id": str(challenge.id),
                        "title": challenge.title,
                        "description": challenge.description,
                        "guild_id": campaign.guild_id,
                        "announcement_channels": campaign.announcement_channels,
                        "order_position": challenge.order_position,
                        "released_at": (
                            challenge.released_at.isoformat()
                            if challenge.released_at
                            else None
                        ),
                        "campaign": {
                            "id": str(campaign.id),
                            "title": campaign.title,
                            "start_time": campaign.start_time.isoformat(),
                            "release_cadence_hours": campaign.release_cadence_hours,
                        },
                    }
                )
            return {"challenges": challenge_list}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve pending announcements")
        except Exception:
            raise plain_error(500, "Internal server error")

    @post(
        "/{challenge_id:str}/mark-released",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def mark_challenge_released(
        self,
        db_session: AsyncSession,
        challenge_id: str,
    ) -> dict[str, bool]:
        """Mark a challenge as released."""
        parsed_challenge_id = _parse_uuid_path(challenge_id, "challenge_id")
        try:
            campaign_ops = CampaignOperations(db_session)
            success = await campaign_ops.mark_challenge_released(parsed_challenge_id)
            if not success:
                raise plain_error(404, "Challenge not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to mark challenge as released")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post(
        "/{challenge_id:str}/mark-announced",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def mark_challenge_announced(
        self,
        db_session: AsyncSession,
        challenge_id: str,
    ) -> dict[str, bool]:
        """Mark a challenge as announced to Discord channels."""
        parsed_challenge_id = _parse_uuid_path(challenge_id, "challenge_id")
        try:
            campaign_ops = CampaignOperations(db_session)
            success = await campaign_ops.mark_challenge_announced(parsed_challenge_id)
            if not success:
                raise plain_error(404, "Challenge not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to mark challenge as announced")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/scoreboard", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_scoreboard(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> dict[str, Any]:
        """Return the squad scoreboard for the most recently begun campaign."""
        try:
            campaign_ops = CampaignOperations(db_session)
            current_campaign = await campaign_ops.get_most_recent_campaign(guild_id)

            if not current_campaign:
                return {
                    "campaign": None,
                    "scoreboard": [],
                    "total_submissions": 0,
                    "total_challenges": 0,
                }

            submission_ops = ChallengeSubmissionOperations(db_session)
            scoreboard_data = await submission_ops.get_campaign_scoreboard(
                current_campaign.id
            )
            total_submissions = await submission_ops.get_campaign_submission_count(
                current_campaign.id
            )
            total_challenges = await campaign_ops.get_campaign_challenge_count(
                current_campaign.id
            )

            campaign_data = {
                "id": str(current_campaign.id),
                "name": current_campaign.title,
                "start_date": (
                    current_campaign.start_time.strftime("%B %d, %Y")
                    if current_campaign.start_time
                    else None
                ),
                "start_time": (
                    current_campaign.start_time.isoformat()
                    if current_campaign.start_time
                    else None
                ),
                "end_date": None,
                "is_active": current_campaign.is_active,
                "guild_id": current_campaign.guild_id,
                "release_cadence_hours": current_campaign.release_cadence_hours,
                "num_challenges": total_challenges,
            }

            formatted_scoreboard = [
                {
                    "squad_name": entry["squad_name"],
                    "total_points": entry["total_points"] or 0,
                    "successful_submissions": entry["successful_submissions"] or 0,
                    "squad_id": str(entry["squad_id"]),
                }
                for entry in scoreboard_data
            ]

            return {
                "campaign": campaign_data,
                "scoreboard": formatted_scoreboard,
                "total_submissions": total_submissions,
                "total_challenges": total_challenges,
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve scoreboard data")
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/upcoming-campaign", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_upcoming_campaign(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> dict[str, Any]:
        """Return the next upcoming campaign for a guild, or None."""
        try:
            query = (
                select(Campaign)
                .where(
                    and_(
                        Campaign.guild_id == guild_id,
                        Campaign.start_time > datetime.now(timezone.utc),
                    )
                )
                .order_by(Campaign.start_time.asc())
                .limit(1)
            )

            result = await db_session.execute(query)
            upcoming_campaign = result.scalar_one_or_none()

            if not upcoming_campaign:
                return {"campaign": None}

            campaign_data = {
                "id": str(upcoming_campaign.id),
                "name": upcoming_campaign.title,
                "start_date": (
                    upcoming_campaign.start_time.strftime("%B %d, %Y at %I:%M %p UTC")
                    if upcoming_campaign.start_time
                    else None
                ),
                "description": upcoming_campaign.description,
                "guild_id": upcoming_campaign.guild_id,
            }
            return {"campaign": campaign_data}
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/detailed-scoreboard", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_detailed_scoreboard(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> dict[str, Any]:
        """Return the detailed campaign scoreboard with per-challenge breakdown."""
        try:
            campaign_ops = CampaignOperations(db_session)
            current_campaign = await campaign_ops.get_most_recent_campaign(guild_id)

            if not current_campaign:
                return {
                    "campaign": None,
                    "detailed_scoreboard": [],
                    "total_submissions": 0,
                    "total_challenges": 0,
                }

            submission_ops = ChallengeSubmissionOperations(db_session)
            detailed_data = await submission_ops.get_detailed_campaign_scoreboard(
                current_campaign.id
            )
            total_submissions = await submission_ops.get_campaign_submission_count(
                current_campaign.id
            )
            total_challenges = await campaign_ops.get_campaign_challenge_count(
                current_campaign.id
            )

            campaign_data = {
                "id": str(current_campaign.id),
                "name": current_campaign.title,
                "start_date": (
                    current_campaign.start_time.strftime("%B %d, %Y")
                    if current_campaign.start_time
                    else None
                ),
                "start_time": (
                    current_campaign.start_time.isoformat()
                    if current_campaign.start_time
                    else None
                ),
                "end_date": None,
                "is_active": current_campaign.is_active,
                "guild_id": current_campaign.guild_id,
                "release_cadence_hours": current_campaign.release_cadence_hours,
                "num_challenges": total_challenges,
            }

            return {
                "campaign": campaign_data,
                "detailed_scoreboard": detailed_data,
                "total_submissions": total_submissions,
                "total_challenges": total_challenges,
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve detailed scoreboard data")
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{challenge_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_challenge(
        self,
        db_session: AsyncSession,
        challenge_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return a challenge with its campaign data."""
        parsed_challenge_id = _parse_uuid_path(challenge_id, "challenge_id")
        try:
            campaign_ops = CampaignOperations(db_session)
            challenge = await campaign_ops.get_challenge_with_campaign(parsed_challenge_id)

            if not challenge:
                raise plain_error(404, "Challenge not found")

            campaign = challenge.campaign
            challenge_data = {
                "id": str(challenge.id),
                "title": challenge.title,
                "description": challenge.description,
                "order_position": challenge.order_position,
                "is_released": challenge.is_released,
                "is_announced": challenge.is_announced,
                "released_at": (
                    challenge.released_at.isoformat() if challenge.released_at else None
                ),
                "announced_at": (
                    challenge.announced_at.isoformat()
                    if challenge.announced_at
                    else None
                ),
                "created_at": challenge.created_at.isoformat(),
                "guild_id": campaign.guild_id,
                "announcement_channels": campaign.announcement_channels,
                "campaign": {
                    "id": str(campaign.id),
                    "title": campaign.title,
                    "start_time": campaign.start_time.isoformat(),
                    "release_cadence_hours": campaign.release_cadence_hours,
                    "is_active": campaign.is_active,
                },
            }
            return {"challenge": challenge_data}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve challenge")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get(
        "/{challenge_id:str}/input-exists",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def check_challenge_input_exists(
        self,
        db_session: AsyncSession,
        challenge_id: str,
        guild_id: str,
        user_id: str,
    ) -> dict[str, bool]:
        """Report whether input already exists for a user's squad."""
        parsed_challenge_id = _parse_uuid_path(challenge_id, "challenge_id")
        try:
            squad_ops = SquadOperations()
            user_squad = await squad_ops.get_user_squad(db_session, guild_id, user_id)

            if not user_squad:
                raise plain_error(404, "User is not a member of any squad")

            input_ops = ChallengeInputOperations(db_session)
            existing_input = await input_ops.get_existing_input(
                parsed_challenge_id, user_squad.id
            )
            return {"exists": existing_input is not None}
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{challenge_id:str}/input", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_challenge_input(
        self,
        db_session: AsyncSession,
        challenge_id: str,
        guild_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Return (generating if needed) the squad's input for a challenge."""
        parsed_challenge_id = _parse_uuid_path(challenge_id, "challenge_id")
        try:
            squad_ops = SquadOperations()
            user_squad = await squad_ops.get_user_squad(db_session, guild_id, user_id)

            if not user_squad:
                raise plain_error(404, "User is not a member of any squad")

            campaign_ops = CampaignOperations(db_session)
            challenge = await campaign_ops.get_challenge_with_campaign(parsed_challenge_id)

            if not challenge:
                raise plain_error(404, "Challenge not found")

            if challenge.campaign.guild_id != guild_id:
                raise plain_error(403, "Challenge does not belong to the specified guild")

            if not challenge.is_released:
                raise plain_error(403, "Challenge has not been released yet")

            if not challenge.input_generator_script:
                raise plain_error(
                    404,
                    "This challenge does not have input generation configured yet. "
                    "Please contact an administrator.",
                )

            input_ops = ChallengeInputOperations(db_session)
            try:
                input_data, _ = await input_ops.get_or_create_input(
                    challenge_id=parsed_challenge_id,
                    squad_id=user_squad.id,
                    script=challenge.input_generator_script,
                )
            except ScriptExecutionError:
                raise plain_error(
                    500,
                    "Failed to generate challenge input due to script execution error",
                )

            return {
                "input_data": input_data,
                "challenge": {
                    "id": str(challenge.id),
                    "title": challenge.title,
                    "description": challenge.description,
                    "order_position": challenge.order_position,
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
            raise plain_error(500, "Failed to retrieve challenge input")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post(
        "/{challenge_id:str}/submit-solution",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def submit_solution(
        self,
        db_session: AsyncSession,
        challenge_id: str,
        data: SolutionSubmissionRequest,
    ) -> dict[str, Any]:
        """Submit a solution for a challenge and score it for the squad."""
        parsed_challenge_id = _parse_uuid_path(challenge_id, "challenge_id")
        try:
            squad_ops = SquadOperations()
            user_squad = await squad_ops.get_user_squad(
                db_session, data.guild_id, data.user_id
            )

            if not user_squad:
                raise plain_error(404, "User is not a member of any squad")

            campaign_ops = CampaignOperations(db_session)
            challenge = await campaign_ops.get_challenge_with_campaign(parsed_challenge_id)

            if not challenge:
                raise plain_error(404, "Challenge not found")

            if challenge.campaign.guild_id != data.guild_id:
                raise plain_error(403, "Challenge does not belong to the specified guild")

            if not challenge.is_released:
                raise plain_error(403, "Challenge has not been released yet")

            submission_ops = ChallengeSubmissionOperations(db_session)
            is_correct, is_first_success, points_earned = (
                await submission_ops.submit_solution(
                    challenge_id=parsed_challenge_id,
                    squad_id=user_squad.id,
                    user_id=data.user_id,
                    username=data.username,
                    submitted_solution=data.submitted_solution,
                )
            )

            return {
                "is_correct": is_correct,
                "is_first_success": is_first_success,
                "points_earned": points_earned,
                "challenge": {
                    "id": str(challenge.id),
                    "title": challenge.title,
                },
                "squad": {
                    "id": str(user_squad.id),
                    "name": user_squad.name,
                },
                "submitted_at": "just_now",
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to submit solution")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")
