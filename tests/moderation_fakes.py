"""A fake Discord for the moderation gateway, and a database to run it on.

:class:`FakeModerationDiscord` is a :class:`ModerationPort` over an in-memory
guild: members with timeouts, bans, messages, and the moderators' channel. It
records every call that changes Discord in ``calls`` so a test can count
bans, timeouts and deletes, and every post or edit in ``posts`` / ``edits``.

Failures are injected per method: ``fail[name] = exception`` raises before
the effect (Discord refused, or the request never arrived), and
``crash_after[name] = exception`` applies the effect and then raises (the
response was lost).

:func:`gateway_session_factory` builds a fresh SQLite database file with the
gateway's tables; separate connections per session, like processes sharing
Postgres.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.shared.database import Base
from smarter_dev.web import models
from smarter_dev.web.moderation_gateway.types import DeleteResult
from smarter_dev.web.moderation_gateway.types import EffectNotFound
from smarter_dev.web.moderation_gateway.types import EffectRejected
from smarter_dev.web.moderation_gateway.types import TimeoutState

GUILD = "644299523686006834"
BOT_ID = "1000"
REPORT_CHANNEL = "728249959098482829"

GATEWAY_TABLES = [
    models.ModerationIncident.__table__,
    models.ModerationIncidentLink.__table__,
    models.ModerationIncidentEntry.__table__,
    models.ModerationOperation.__table__,
    models.ModerationOutbox.__table__,
    models.ModerationMemberLock.__table__,
    models.ModerationAction.__table__,
    models.SpamRingIncident.__table__,
]


async def gateway_session_factory(directory: Path):
    """A session factory over a new SQLite file holding the gateway's tables."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{directory / 'moderation.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_conn, _record):  # pragma: no cover - wiring
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=GATEWAY_TABLES)
    return engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@dataclass
class FakeMessage:
    channel_id: str
    message_id: str
    author_id: str
    content: str = ""
    is_bot: bool = False
    webhook_id: str | None = None


@dataclass
class FakeModerationDiscord:
    """One guild, as the gateway's port sees it."""

    guild_id: str = GUILD
    members: dict[str, float | None] = field(default_factory=dict)
    bans: set[str] = field(default_factory=set)
    messages: dict[str, FakeMessage] = field(default_factory=dict)
    protected: dict[str, str] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    posts: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    announced: list[Any] = field(default_factory=list)
    fail: dict[str, BaseException] = field(default_factory=dict)
    crash_after: dict[str, BaseException] = field(default_factory=dict)
    #: Awaited inside each effect, so a test can interleave two actors.
    pause: dict[str, asyncio.Event] = field(default_factory=dict)
    #: Set when an effect reaches its pause.
    started: dict[str, asyncio.Event] = field(default_factory=dict)
    clock: Any = None
    _next_id: int = 900_000

    # -- helpers for tests --------------------------------------------------

    def add_member(self, user_id: str, *, until: float | None = None) -> None:
        self.members[str(user_id)] = until

    def post(
        self, channel_id: str, message_id: str, author_id: str, content: str = ""
    ) -> FakeMessage:
        message = FakeMessage(str(channel_id), str(message_id), str(author_id), content)
        self.messages[str(message_id)] = message
        return message

    def count(self, name: str, target: str | None = None) -> int:
        return sum(
            1
            for call in self.calls
            if call[0] == name and (target is None or call[1] == target)
        )

    def now(self) -> float:
        return self.clock() if self.clock else datetime.now(UTC).timestamp()

    async def _gate(self, name: str) -> None:
        if name in self.fail:
            raise self.fail.pop(name)
        if name in self.pause:
            self.started.setdefault(name, asyncio.Event()).set()
            await self.pause[name].wait()

    def _after(self, name: str) -> None:
        if name in self.crash_after:
            raise self.crash_after.pop(name)

    # -- ModerationPort -----------------------------------------------------

    async def bot_identity(self):
        return BOT_ID, "smarter-bot"

    async def staff_refusal(self, guild_id, user_id, *, is_bot=None):
        if str(user_id) == BOT_ID:
            return "the bot itself"
        if is_bot:
            return "a bot"
        return self.protected.get(str(user_id))

    async def timeout_state(self, guild_id, user_id):
        if str(user_id) not in self.members:
            return TimeoutState(is_member=False, until=None)
        until = self.members[str(user_id)]
        if until is not None and until <= self.now():
            until = None
        return TimeoutState(is_member=True, until=until)

    async def is_banned(self, guild_id, user_id):
        return str(user_id) in self.bans

    async def message_exists(self, channel_id, message_id):
        return str(message_id) in self.messages

    async def message_author(self, channel_id, message_id):
        message = self.messages.get(str(message_id))
        if message is None:
            return None
        return message.author_id, None, message.is_bot or message.webhook_id is not None

    async def ban(self, guild_id, user_id, *, reason, delete_message_seconds):
        await self._gate("ban")
        self.calls.append(("ban", str(user_id), reason))
        self.bans.add(str(user_id))
        self.members.pop(str(user_id), None)
        self._after("ban")

    async def kick(self, guild_id, user_id, *, reason):
        await self._gate("kick")
        if str(user_id) not in self.members:
            raise EffectNotFound("not a member")
        self.calls.append(("kick", str(user_id), reason))
        self.members.pop(str(user_id), None)
        self._after("kick")

    async def set_timeout(self, guild_id, user_id, until, *, reason):
        await self._gate("set_timeout")
        if str(user_id) not in self.members:
            raise EffectNotFound("not a member")
        stored = round(until.timestamp(), 3) if until is not None else None
        self.calls.append(("timeout", str(user_id), stored))
        self.members[str(user_id)] = stored
        self._after("set_timeout")
        return stored

    async def delete_messages(self, channel_id, message_ids):
        await self._gate("delete_messages")
        result = DeleteResult()
        for message_id in message_ids:
            if str(message_id) in self.messages:
                self.calls.append(
                    (
                        "delete",
                        self.messages[str(message_id)].author_id,
                        str(message_id),
                    )
                )
                del self.messages[str(message_id)]
                result.deleted.append(str(message_id))
            else:
                result.missing.append(str(message_id))
        self._after("delete_messages")
        return result

    async def send_message(
        self, channel_id, content, *, role_mention_ids=None, attachment=None
    ):
        await self._gate("send_message")
        self._next_id += 1
        message_id = str(self._next_id)
        self.posts.append(
            {
                "channel_id": str(channel_id),
                "message_id": message_id,
                "content": content,
                "role_mention_ids": role_mention_ids,
                "attachment": attachment,
            }
        )
        self._after("send_message")
        return message_id

    async def edit_message(self, channel_id, message_id, content, *, attachment=None):
        await self._gate("edit_message")
        self.edits.append(
            {
                "channel_id": str(channel_id),
                "message_id": str(message_id),
                "content": content,
                "attachment": attachment,
            }
        )

    async def find_message(self, channel_id, marker):
        for post in reversed(self.posts):
            if post["channel_id"] == str(channel_id) and marker in post["content"]:
                return post["message_id"]
        return None

    async def announce(self, action):
        self.announced.append(action)

    # -- views ----------------------------------------------------------------

    def reports(self, channel_id: str = REPORT_CHANNEL) -> list[dict]:
        return [post for post in self.posts if post["channel_id"] == channel_id]

    def latest_report_text(self, channel_id: str = REPORT_CHANNEL) -> str:
        reports = self.reports(channel_id)
        assert reports, "no report was posted"
        message_id = reports[-1]["message_id"]
        edits = [edit for edit in self.edits if edit["message_id"] == message_id]
        return edits[-1]["content"] if edits else reports[-1]["content"]


def refused(status: int = 403) -> EffectRejected:
    return EffectRejected(status, "Discord refused it (missing permission)")


class FakeDiscordRest:
    """Discord's REST API for one guild, as an ``httpx.MockTransport`` handler.

    Answers the reads the gateway's REST port makes (the member, the ban, the
    message, ``/users/@me``) from its state, applies the writes, and records
    every write in ``writes``. ``fail_writes`` answers every write with that
    status instead (404, 403, 500…), changing nothing.
    """

    def __init__(self, guild_id: str = "G1", *, fail_writes: int | None = None) -> None:
        self.guild_id = guild_id
        self.members: dict[str, str | None] = {}
        self.bans: set[str] = set()
        self.messages: dict[str, dict] = {}
        self.writes: list = []
        self.fail_writes = fail_writes

    def transport(self):
        import httpx

        return httpx.MockTransport(self.handle)

    def add_member(self, user_id: str, until: str | None = None) -> None:
        self.members[str(user_id)] = until

    def handle(self, request):
        import httpx

        path = request.url.path.removeprefix("/api/v10")
        method = request.method
        parts = path.strip("/").split("/")
        if method == "GET":
            if path == "/users/@me":
                return httpx.Response(
                    200, json={"id": BOT_ID, "username": "smarter-bot"}
                )
            if parts[:1] == ["guilds"] and parts[2:3] == ["bans"]:
                return httpx.Response(200 if parts[3] in self.bans else 404, json={})
            if (
                parts[:1] == ["guilds"]
                and parts[2:3] == ["members"]
                and len(parts) == 4
            ):
                user_id = parts[3]
                if user_id not in self.members:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json=self._member(user_id))
            if (
                parts[:1] == ["channels"]
                and parts[2:3] == ["messages"]
                and len(parts) == 4
            ):
                message = self.messages.get(parts[3])
                if message is None:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json=message)
            return httpx.Response(404, json={})
        self.writes.append(request)
        if self.fail_writes is not None:
            return httpx.Response(self.fail_writes, json={})
        if parts[:1] == ["guilds"] and parts[2:3] == ["bans"] and method == "PUT":
            self.bans.add(parts[3])
            self.members.pop(parts[3], None)
            return httpx.Response(204)
        if parts[:1] == ["guilds"] and parts[2:3] == ["members"]:
            user_id = parts[3]
            if user_id not in self.members:
                return httpx.Response(404, json={})
            if method == "DELETE":
                del self.members[user_id]
                return httpx.Response(204)
            if method == "PATCH":
                import json

                body = json.loads(request.content or b"{}")
                if "communication_disabled_until" in body:
                    self.members[user_id] = body["communication_disabled_until"]
                return httpx.Response(200, json=self._member(user_id))
        if (
            parts[:1] == ["channels"]
            and parts[2:3] == ["messages"]
            and method == "DELETE"
        ):
            if self.messages.pop(parts[3], None) is None:
                return httpx.Response(404, json={})
            return httpx.Response(204)
        if parts[:1] == ["channels"] and parts[-1] == "messages" and method == "POST":
            return httpx.Response(200, json={"id": str(900_000 + len(self.writes))})
        return httpx.Response(204)

    def _member(self, user_id: str) -> dict:
        return {
            "user": {"id": user_id, "username": f"user{user_id}"},
            "roles": [],
            "communication_disabled_until": self.members.get(user_id),
        }
