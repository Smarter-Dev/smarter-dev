"""Bot API for code jams.

``GET /api/guilds/{guild_id}/jam`` tells the bot which jam is on and where its
channels are. The routes under it keep the jam's teams and showcase entries;
the rules are in :mod:`smarter_dev.web.jams.teams`. A team change the rules
refuse is still a 200: the body says ``"ok": false`` and why, because the
bot's API client treats every 4xx as a failure and reads no body from it.

The bot polls ``POST /api/jam-messages/claim`` and posts what it gets back,
then reports each message with ``mark-sent`` or ``mark-failed``. Claiming
before posting means a message is handed out once. It can still land in the
channel twice when the bot's REST client retries a timeout or a 5xx; see
:mod:`smarter_dev.web.jams.store`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Controller
from litestar import get
from litestar import post
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import Field
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.jams import store
from smarter_dev.web.jams import teams
from smarter_dev.web.models import JAM_CHANNEL_COLUMNS

# Declared per route, like the other bot API controllers: Skrift's auth guard
# looks for the ``APIKeyOnly`` marker on the route handler itself.
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission("bot-api")]


class MarkJamMessageFailedRequest(BaseModel):
    """Why the bot could not post a claimed message."""

    error: str = Field(min_length=1, max_length=2000)


# A Discord id: digits only.
_DISCORD_ID = r"^[0-9]{1,22}$"

NO_JAM = "no_jam"


class CreateJamTeamRequest(BaseModel):
    """A team thread the bot just made, and the member who asked for it."""

    name: str = Field(min_length=1, max_length=100)
    thread_id: str = Field(pattern=_DISCORD_ID)
    user_id: str = Field(pattern=_DISCORD_ID)


class AddJamTeamMemberRequest(BaseModel):
    """Who to add to a team, and the team member adding them."""

    user_id: str = Field(pattern=_DISCORD_ID)
    invited_by: str = Field(pattern=_DISCORD_ID)


class LeaveJamTeamRequest(BaseModel):
    """The member leaving a team."""

    user_id: str = Field(pattern=_DISCORD_ID)


class RecordJamSubmissionRequest(BaseModel):
    """A new post in the showcase forum."""

    thread_id: str = Field(pattern=_DISCORD_ID)
    author_id: str = Field(pattern=_DISCORD_ID)


def _team_json(team: teams.TeamInfo | None) -> dict[str, Any] | None:
    if team is None:
        return None
    return {
        "name": team.name,
        "thread_id": team.thread_id,
        "member_ids": list(team.member_ids),
    }


def _team_outcome_json(outcome: teams.TeamOutcome) -> dict[str, Any]:
    return {
        "ok": outcome.refusal is None,
        "reason": outcome.refusal,
        "team": _team_json(outcome.team),
        "already": outcome.already,
        "members_left": outcome.members_left,
    }


_NO_JAM_BODY: dict[str, Any] = {"ok": False, "reason": NO_JAM, "team": None}


def _parse_message_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as parse_error:
        raise ValidationException(detail="Invalid message_id format") from parse_error


class JamMessageController(Controller):
    """Claim and report scheduled jam messages."""

    path = "/api/jam-messages"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/claim", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def claim_due_jam_messages(
        self, db_session: AsyncSession
    ) -> dict[str, list[dict[str, Any]]]:
        """Claim every due message; each is returned to exactly one caller."""
        messages = await store.claim_due_messages(db_session)
        return {
            "messages": [
                {
                    "id": str(message.id),
                    "guild_id": message.guild_id,
                    "channel_id": message.channel_id,
                    "content": message.content,
                    "role_id": message.role_id,
                }
                for message in messages
            ]
        }

    @post("/{message_id:str}/mark-sent", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def mark_jam_message_sent(
        self, db_session: AsyncSession, message_id: str
    ) -> dict[str, bool]:
        """Record a claimed message as posted."""
        if not await store.mark_message_sent(db_session, _parse_message_id(message_id)):
            raise plain_error(404, "Jam message not found or not claimed")
        return {"success": True}

    @post(
        "/{message_id:str}/mark-failed", status_code=HTTP_200_OK, guards=BOT_API_GUARDS
    )
    async def mark_jam_message_failed(
        self,
        db_session: AsyncSession,
        message_id: str,
        data: MarkJamMessageFailedRequest,
    ) -> dict[str, bool]:
        """Record a claimed message as failed, with the bot's reason."""
        marked = await store.mark_message_failed(
            db_session, _parse_message_id(message_id), data.error
        )
        if not marked:
            raise plain_error(404, "Jam message not found or not claimed")
        return {"success": True}


class GuildJamController(Controller):
    """What the bot needs to know about a guild's jam."""

    path = "/api/guilds/{guild_id:str}/jam"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/", guards=BOT_API_GUARDS)
    async def get_guild_jam(
        self, db_session: AsyncSession, guild_id: str
    ) -> dict[str, Any]:
        """The guild's jam channels and the jam members can take part in now.

        ``jam`` is ``None`` between jams, and every id is ``None`` until setup
        has made that piece.
        """
        setup = await store.get_setup(db_session, guild_id)
        jam = await store.current_jam(db_session, guild_id)
        return {
            "channels": {
                key: setup.channel_id_for(key) if setup else None
                for key in JAM_CHANNEL_COLUMNS
            },
            "role_id": setup.role_id if setup else None,
            "jam": None
            if jam is None
            else {
                "id": str(jam.id),
                "name": jam.name,
                "max_team_size": jam.max_team_size,
                "starts_at": store.as_utc(jam.starts_at).isoformat(),
                "submissions_close_at": store.as_utc(
                    jam.submissions_close_at
                ).isoformat(),
                "showcase_tag_id": jam.showcase_tag_id,
                "year_tag_id": jam.year_tag_id,
            },
        }

    @get("/members/{user_id:str}", guards=BOT_API_GUARDS)
    async def get_jam_member(
        self, db_session: AsyncSession, guild_id: str, user_id: str
    ) -> dict[str, Any]:
        """The member's team in the current jam, and whether they may join one.

        ``reason`` is why they cannot make or join a team now; ``None`` if
        they can.
        """
        jam = await store.current_jam(db_session, guild_id)
        if jam is None:
            return dict(_NO_JAM_BODY)
        refused = await teams.cannot_join(db_session, jam, user_id)
        team = await teams.team_of(db_session, jam.id, user_id)
        return {
            "ok": refused is None,
            "reason": refused.refusal if refused else None,
            "team": _team_json(teams.team_info(team)),
        }

    @post("/teams", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def create_jam_team(
        self, db_session: AsyncSession, guild_id: str, data: CreateJamTeamRequest
    ) -> dict[str, Any]:
        """Record a team for the thread the bot made."""
        jam = await store.current_jam(db_session, guild_id)
        if jam is None:
            return dict(_NO_JAM_BODY)
        outcome = await teams.create_team(
            db_session,
            jam,
            name=data.name,
            thread_id=data.thread_id,
            user_id=data.user_id,
        )
        return _team_outcome_json(outcome)

    @post(
        "/teams/{thread_id:str}/members",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def add_jam_team_member(
        self,
        db_session: AsyncSession,
        guild_id: str,
        thread_id: str,
        data: AddJamTeamMemberRequest,
    ) -> dict[str, Any]:
        """Add a member to the team whose thread this is."""
        jam = await store.current_jam(db_session, guild_id)
        if jam is None:
            return dict(_NO_JAM_BODY)
        outcome = await teams.add_member(
            db_session,
            jam,
            thread_id=thread_id,
            user_id=data.user_id,
            invited_by=data.invited_by,
        )
        return _team_outcome_json(outcome)

    @post(
        "/teams/{thread_id:str}/leave",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def leave_jam_team(
        self,
        db_session: AsyncSession,
        guild_id: str,
        thread_id: str,
        data: LeaveJamTeamRequest,
    ) -> dict[str, Any]:
        """Take a member off the team whose thread this is."""
        jam = await store.current_jam(db_session, guild_id)
        if jam is None:
            return dict(_NO_JAM_BODY)
        outcome = await teams.leave_team(
            db_session, jam, thread_id=thread_id, user_id=data.user_id
        )
        return _team_outcome_json(outcome)

    @post("/submissions", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def record_jam_submission(
        self,
        db_session: AsyncSession,
        guild_id: str,
        data: RecordJamSubmissionRequest,
    ) -> dict[str, Any]:
        """Record a showcase post against its author's team.

        ``entry_thread_id`` is the post that counts for that team or solo
        member. ``new`` is false when this post was recorded before.
        """
        jam = await store.current_jam(db_session, guild_id)
        outcome = (
            None
            if jam is None
            else await teams.record_submission(
                db_session, jam, thread_id=data.thread_id, author_id=data.author_id
            )
        )
        if outcome is None:
            return dict(_NO_JAM_BODY)
        return {
            "ok": True,
            "reason": None,
            "team": _team_json(outcome.team),
            "entry_thread_id": outcome.entry_thread_id,
            "new": outcome.new,
        }

    @post(
        "/submissions/{thread_id:str}/remove",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def remove_jam_submission(
        self, db_session: AsyncSession, guild_id: str, thread_id: str
    ) -> dict[str, bool]:
        """Forget a showcase post that was deleted in Discord."""
        removed = await teams.remove_submission(db_session, guild_id, thread_id)
        return {"removed": removed}
