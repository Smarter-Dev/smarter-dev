"""Native Litestar port of the Advent of Code bot API (legacy ``routers/advent_of_code.py``).

Ports the legacy FastAPI ``routers/advent_of_code.py`` (prefix ``/advent-of-code``)
— part of unit U6 in docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact
paths, verbs, status codes, and request/response shapes of the FastAPI
implementation so ``smarter_dev/bot/services/advent_of_code_service.py`` (and any
external caller) needs zero changes.

NOT registered in ``app.yaml`` yet — the FastAPI mount still owns ``/api``. This
module exists for isolated parity tests until the atomic switchover.

Session note: post phase-02 the single injected ``db_session`` serves the AoC
tables. ``AdventOfCodeConfigOperations`` takes the session per-method (not in its
constructor), so each handler passes ``db_session`` through.

Error-shape parity: the legacy router answered every failure with a bare
``HTTPException`` — a plain ``{"detail": "<string>"}`` body — so this port raises
:func:`errors.plain_error` with the identical status codes and detail strings.
Notably the legacy ``record_posted_thread`` caught :class:`ConflictError` and
converted it into a plain 409 ``{"detail": "Thread already recorded for this
day"}``; letting that exception reach the flat ``handle_conflict`` handler would
change the wire shape, so it is caught explicitly here.

Status-code parity note: FastAPI defaults every verb (including ``POST``) to 200,
so ``POST /advent-of-code/{guild_id}/threads`` declares ``HTTP_200_OK`` rather
than Litestar's default 201.

Rate-limiting parity is deferred to the switchover commit (see the plan's
"Rate-limiting parity" section); the FastAPI mount still enforces those windows
in production until switchover.
"""

from __future__ import annotations

from typing import Any

from litestar import Controller, get, post
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard

from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    plain_error,
)
from smarter_dev.web.crud import (
    AdventOfCodeConfigOperations,
    ConflictError,
    DatabaseOperationError,
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class RecordThreadRequest(BaseModel):
    """Request model for recording a posted thread."""

    year: int = Field(..., description="Advent of Code year")
    day: int = Field(..., ge=1, le=25, description="Day of the challenge (1-25)")
    thread_id: str = Field(..., description="Discord thread ID")
    thread_title: str = Field(..., description="Thread title")


class ThreadResponse(BaseModel):
    """Response model for thread data."""

    id: str
    guild_id: str
    year: int
    day: int
    thread_id: str
    thread_title: str
    created_at: str


class ConfigResponse(BaseModel):
    """Response model for AoC configuration data."""

    guild_id: str
    forum_channel_id: str | None
    is_active: bool


def _thread_data(thread: Any) -> dict[str, Any]:
    """Serialize a thread row into the nested ``thread`` object the bot reads."""
    return {
        "id": str(thread.id),
        "guild_id": thread.guild_id,
        "year": thread.year,
        "day": thread.day,
        "thread_id": thread.thread_id,
        "thread_title": thread.thread_title,
        "created_at": thread.created_at.isoformat(),
    }


class AdventOfCodeController(Controller):
    """Advent of Code endpoints — active configs, guild config, thread tracking."""

    path = "/api/advent-of-code"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/active-configs", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_active_configs(
        self,
        db_session: AsyncSession,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return all active Advent of Code configurations for the bot."""
        try:
            aoc_ops = AdventOfCodeConfigOperations()
            configs = await aoc_ops.get_active_configs(db_session)

            config_list = [
                {
                    "guild_id": config.guild_id,
                    "forum_channel_id": config.forum_channel_id,
                    "is_active": config.is_active,
                }
                for config in configs
            ]
            return {"configs": config_list}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve active configurations")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{guild_id:str}/config", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_guild_config(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> ConfigResponse:
        """Return (creating if absent) the AoC configuration for a guild."""
        try:
            aoc_ops = AdventOfCodeConfigOperations()
            config = await aoc_ops.get_or_create_config(db_session, guild_id)

            return ConfigResponse(
                guild_id=config.guild_id,
                forum_channel_id=config.forum_channel_id,
                is_active=config.is_active,
            )
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve configuration")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get(
        "/{guild_id:str}/threads/{year:int}/{day:int}",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def get_posted_thread(
        self,
        db_session: AsyncSession,
        guild_id: str,
        year: int,
        day: int,
    ) -> dict[str, Any]:
        """Return the recorded thread for a specific day, or 404 if none."""
        try:
            aoc_ops = AdventOfCodeConfigOperations()
            thread = await aoc_ops.get_posted_thread(db_session, guild_id, year, day)

            if thread is None:
                raise plain_error(404, "Thread not found")

            return {"thread": _thread_data(thread)}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to check posted thread")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post("/{guild_id:str}/threads", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def record_posted_thread(
        self,
        db_session: AsyncSession,
        guild_id: str,
        data: RecordThreadRequest,
    ) -> dict[str, Any]:
        """Record that a thread has been posted for a specific day."""
        try:
            aoc_ops = AdventOfCodeConfigOperations()
            thread = await aoc_ops.record_posted_thread(
                db_session,
                guild_id=guild_id,
                year=data.year,
                day=data.day,
                thread_id=data.thread_id,
                thread_title=data.thread_title,
            )
            # Serialize before commit to avoid session-detachment issues with the
            # Skrift-injected session (see the bytes controller precedent).
            response = {"success": True, "thread": _thread_data(thread)}
            await db_session.commit()
            return response
        except ConflictError:
            raise plain_error(409, "Thread already recorded for this day")
        except DatabaseOperationError:
            raise plain_error(500, "Failed to record posted thread")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{guild_id:str}/threads", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_guild_threads(
        self,
        db_session: AsyncSession,
        guild_id: str,
        year: int | None = None,
    ) -> dict[str, list[ThreadResponse]]:
        """Return all recorded threads for a guild, optionally filtered by year."""
        try:
            aoc_ops = AdventOfCodeConfigOperations()
            threads = await aoc_ops.get_guild_threads(db_session, guild_id, year)

            thread_responses = [
                ThreadResponse(
                    id=str(thread.id),
                    guild_id=thread.guild_id,
                    year=thread.year,
                    day=thread.day,
                    thread_id=thread.thread_id,
                    thread_title=thread.thread_title,
                    created_at=thread.created_at.isoformat(),
                )
                for thread in threads
            ]
            return {"threads": thread_responses}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve guild threads")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")
