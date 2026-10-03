"""Tests for a chat bot purge request from open to receipt.

Real SQLite tables and a fake Redis; the purge agent is scripted. Synthetic
members only: ``kai`` (111…) is purged, ``nia`` (222…) is not.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.shared.privacy_purge import PurgeAck
from smarter_dev.shared.privacy_purge import PurgeCommand
from smarter_dev.shared.privacy_purge import enforcing_key
from smarter_dev.web.chat_bot_purge import STATUS_AWAITING_ACKS
from smarter_dev.web.chat_bot_purge import STATUS_CHECKING
from smarter_dev.web.chat_bot_purge import STATUS_CLOSED
from smarter_dev.web.chat_bot_purge import STATUS_COMPLETE
from smarter_dev.web.chat_bot_purge import STATUS_NEEDS_REVIEW
from smarter_dev.web.chat_bot_purge import STATUS_WAITING
from smarter_dev.web.chat_bot_purge import close_request
from smarter_dev.web.chat_bot_purge import open_purge_request
from smarter_dev.web.chat_bot_purge import read_blocked_users
from smarter_dev.web.chat_bot_purge import record_ack
from smarter_dev.web.chat_bot_purge import run_check
from smarter_dev.web.chat_bot_purge import run_purge
from smarter_dev.web.chat_memory_purge import NoteEdit
from smarter_dev.web.chat_memory_purge import PurgeContext
from smarter_dev.web.chat_memory_purge import PurgeOutput
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotPurgeRequest

_GUILD = "123456789012345678"
_KAI_ID = "111111111111111111"
_NIA_ID = "222222222222222222"
_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

KAI_LINE = f"- kai (id {_KAI_ID}) is deep in embedded rust."
NIA_LINE = f"- nia (id {_NIA_ID}) opens with a wind-up every time."
MEMORY = f"## People & Relationships\n{NIA_LINE}\n{KAI_LINE}"


class _Result:
    def __init__(self, output):
        self.output = output


class _ScriptedAgent:
    async def run(self, user_prompt: str, *, deps: PurgeContext):
        return _Result(
            PurgeOutput(
                memory=deps.memory.replace(f"\n{KAI_LINE}", ""),
                behavior=deps.behavior,
                personality=deps.personality,
                notes=[NoteEdit(id=note_id, action="keep") for note_id, _ in deps.notes],
            )
        )


@pytest.fixture
def session_factory(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    return factory


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


async def _enforce(redis, revision: int) -> None:
    await redis.set(enforcing_key("bot"), revision)
    await redis.set(enforcing_key("worker"), revision)


async def _open(db_session, names=("kai",)) -> ChatBotPurgeRequest:
    request = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=list(names), requested_by="admin-1"
    )
    await db_session.commit()
    return request


async def _no_sleep(_seconds):
    return None


# -- opening -------------------------------------------------------------------------


async def test_opening_a_request_blocks_the_user_and_reopening_restarts_it(db_session):
    first = await _open(db_session)
    blocked = await read_blocked_users(db_session)
    assert blocked.user_ids == [_KAI_ID]
    assert blocked.revision == 1
    first_run = first.run_id

    second = await _open(db_session, names=("Kai the Rustacean",))

    assert second.id == first.id
    assert second.run_id != first_run
    assert second.names == ["kai", "Kai the Rustacean"]
    # Already blocked: the list does not change again.
    assert (await read_blocked_users(db_session)).revision == 1


# -- the run ---------------------------------------------------------------------------


async def test_a_run_waits_for_both_runtimes_to_enforce_the_new_list(
    db_session, session_factory, redis
):
    request = await _open(db_session)
    await redis.set(enforcing_key("bot"), 1)  # the worker has not caught up

    status = await run_purge(
        request.id,
        request.run_id,
        session_factory=session_factory,
        redis=redis,
        now=lambda: _NOW,
        agent=_ScriptedAgent(),
        wait_seconds=10,
        poll_seconds=5,
        sleep=_no_sleep,
    )

    assert status == STATUS_WAITING
    assert await redis.xlen(PURGE_STREAM) == 0


async def test_a_full_run_purges_memory_sends_one_command_and_settles_after_the_check(
    db_session, session_factory, redis
):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=MEMORY,
        notes_consumed=1,
        model_name="stub",
        dreamed_at=_NOW - timedelta(days=1),
    )
    await db_session.commit()
    request = await _open(db_session)
    await _enforce(redis, 1)
    # A chat history the bot has not purged yet, and one about nia only.
    await redis.set("chat_agent:999000111222333444:history", f"kai ({_KAI_ID}) said hi")
    await redis.set("chat_agent:999000111222333445:history", "nia said hi")

    status = await run_purge(
        request.id,
        request.run_id,
        session_factory=session_factory,
        redis=redis,
        now=lambda: _NOW,
        agent=_ScriptedAgent(),
        sleep=_no_sleep,
    )

    assert status == STATUS_AWAITING_ACKS
    memory = await get_guild_memory_blob(db_session, _GUILD)
    await db_session.refresh(memory)
    assert KAI_LINE not in memory.content and NIA_LINE in memory.content
    entries = await redis.xrange(PURGE_STREAM)
    assert len(entries) == 1
    command = PurgeCommand.model_validate_json(entries[0][1][b"payload"])
    assert command.user_id == _KAI_ID
    assert command.guild_ids == [_GUILD]
    assert command.run_id == request.run_id

    for component in ("bot", "worker"):
        async with session_factory() as session:
            acked = await record_ack(
                session,
                request.run_id,
                PurgeAck(component=component, guild_id=_GUILD, outcome="purged"),
            )
            await session.commit()
    assert acked.status == STATUS_CHECKING

    status = await run_check(
        request.id, session_factory=session_factory, redis=redis, now=lambda: _NOW
    )
    assert status == STATUS_NEEDS_REVIEW
    async with session_factory() as session:
        stored = await session.get(ChatBotPurgeRequest, request.id)
        remains = stored.check_report["remains"]
    assert [hit["location"] for hit in remains] == ["chat_agent:999000111222333444:history"]
    assert remains[0]["id_hits"] == 1
    # The report holds locations and counts, never the stored text.
    assert "said hi" not in json.dumps(stored.check_report)
    # Both runtimes acknowledged, so the command carrying the ID is gone.
    assert await redis.xlen(PURGE_STREAM) == 0

    await redis.delete("chat_agent:999000111222333444:history")
    status = await run_check(
        request.id, session_factory=session_factory, redis=redis, now=lambda: _NOW
    )
    assert status == STATUS_COMPLETE


async def test_a_failed_ack_keeps_the_request_in_review(db_session, session_factory, redis):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=MEMORY,
        notes_consumed=1,
        model_name="stub",
        dreamed_at=_NOW - timedelta(days=1),
    )
    await db_session.commit()
    request = await _open(db_session)
    await _enforce(redis, 1)
    await run_purge(
        request.id, request.run_id, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=_ScriptedAgent(), sleep=_no_sleep,
    )
    async with session_factory() as session:
        await record_ack(session, request.run_id, PurgeAck(component="bot", guild_id=_GUILD, outcome="failed"))
        await record_ack(session, request.run_id, PurgeAck(component="worker", guild_id=_GUILD, outcome="unchanged"))
        await session.commit()

    status = await run_check(
        request.id, session_factory=session_factory, redis=redis, now=lambda: _NOW
    )
    assert status == STATUS_NEEDS_REVIEW


async def test_a_superseded_run_touches_nothing(db_session, session_factory, redis):
    request = await _open(db_session)
    old_run = request.run_id
    await _open(db_session)  # the admin submitted again
    await _enforce(redis, 1)

    status = await run_purge(
        request.id, old_run, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=_ScriptedAgent(), sleep=_no_sleep,
    )

    assert status == "superseded"
    assert await redis.xlen(PURGE_STREAM) == 0
    async with session_factory() as session:
        assert await record_ack(
            session, old_run, PurgeAck(component="bot", guild_id=_GUILD, outcome="purged")
        ) is None


# -- closing ---------------------------------------------------------------------------


async def test_closing_leaves_a_bare_receipt_and_keeps_the_block(db_session, redis):
    request = await _open(db_session, names=("kai", "Kai the Rustacean"))

    await close_request(db_session, request.id, now=_NOW, redis=redis)
    await db_session.commit()

    stored = await db_session.get(ChatBotPurgeRequest, request.id)
    assert stored.status == STATUS_CLOSED
    assert stored.discord_user_id is None
    assert stored.names is None
    assert stored.check_report is None
    assert _KAI_ID not in json.dumps(stored.steps)
    assert "kai" not in json.dumps(stored.steps).lower()
    blocked = (await db_session.scalars(select(ChatBotBlockedUser))).all()
    assert [row.discord_user_id for row in blocked] == [_KAI_ID]


# -- the API stays behind the bot key --------------------------------------------------


def test_the_privacy_routes_require_the_bot_api_key():
    from skrift.auth.guards import APIKeyOnly
    from skrift.auth.guards import Permission

    from smarter_dev.web.api_native.auth import bot_api_auth_guard
    from smarter_dev.web.api_native.privacy import PrivacyController

    for handler in (PrivacyController.blocked_users, PrivacyController.acknowledge):
        guards = handler.guards
        assert bot_api_auth_guard in guards
        assert any(isinstance(g, APIKeyOnly) for g in guards)
        assert any(isinstance(g, Permission) for g in guards)


class _InterruptingAgent(_ScriptedAgent):
    """Purges normally, but the admin acts while the model is thinking."""

    def __init__(self, action):
        self.action = action

    async def run(self, user_prompt: str, *, deps: PurgeContext):
        await self.action()
        return await super().run(user_prompt, deps=deps)


async def _seed_memory(db_session):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=MEMORY,
        notes_consumed=1,
        model_name="stub",
        dreamed_at=_NOW - timedelta(days=1),
    )
    await db_session.commit()


async def test_closing_mid_run_stops_the_run_and_leaves_no_command(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)

    async def close():
        async with session_factory() as session:
            await close_request(session, request.id, now=_NOW, redis=redis)
            await session.commit()

    status = await run_purge(
        request.id, request.run_id, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=_InterruptingAgent(close), sleep=_no_sleep,
    )

    assert status == "superseded"
    assert await redis.xlen(PURGE_STREAM) == 0
    async with session_factory() as session:
        stored = await session.get(ChatBotPurgeRequest, request.id)
        assert stored.status == STATUS_CLOSED
        assert stored.discord_user_id is None
        assert _KAI_ID not in json.dumps(stored.steps)
        assert await record_ack(
            session, request.run_id, PurgeAck(component="bot", guild_id=_GUILD, outcome="purged")
        ) is None


async def test_a_resubmit_mid_run_supersedes_it_without_a_stray_command(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    first_run = request.run_id
    await _enforce(redis, 1)

    async def resubmit():
        async with session_factory() as session:
            await open_purge_request(
                session, discord_user_id=_KAI_ID, names=["kai"], requested_by="admin-1"
            )
            await session.commit()

    status = await run_purge(
        request.id, first_run, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=_InterruptingAgent(resubmit), sleep=_no_sleep,
    )

    assert status == "superseded"
    assert await redis.xlen(PURGE_STREAM) == 0


async def test_the_check_reads_list_and_stream_keys_too(db_session, session_factory, redis):
    from smarter_dev.shared.privacy_purge import PurgeTarget
    from smarter_dev.web.chat_bot_purge import scan_stores

    await redis.rpush("chat_agent:999000111222333444:history", "hello", f"kai {_KAI_ID}")
    await redis.xadd("proactive:v1:dead-letter", {"payload": "kai was here"})

    report = await scan_stores(db_session, redis, PurgeTarget.build(_KAI_ID, ["kai"]))

    assert [hit["location"] for hit in report["remains"]] == [
        "chat_agent:999000111222333444:history"
    ]
    assert [hit["location"] for hit in report["operational"]] == ["proactive:v1:dead-letter"]
