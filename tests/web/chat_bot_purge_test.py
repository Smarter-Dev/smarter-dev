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
from sqlalchemy.orm.attributes import flag_modified

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
from smarter_dev.web.chat_memory_purge import PurgeContext
from smarter_dev.web.chat_memory_purge import PurgeOutput
from smarter_dev.web.chat_memory_purge import SegmentEdit
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
    """Removes every segment that names kai."""

    async def run(self, user_prompt: str, *, deps: PurgeContext):
        return _Result(
            PurgeOutput(
                edits=[
                    SegmentEdit(id=seg.id, action="remove")
                    for segs in deps.editable_by_location().values()
                    for seg in segs
                ]
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
    for component in ("bot", "worker"):
        await redis.set(f"{enforcing_key(component)}:host-1", revision, ex=180)
        await redis.set(f"privacy:v1:consumer:{component}:host-1", "1", ex=180)


async def _open(db_session, names=("kai",)) -> ChatBotPurgeRequest:
    request = await open_purge_request(
        db_session, discord_user_id=_KAI_ID, names=list(names), requested_by="admin-1"
    )
    await db_session.commit()
    return request


# The structured fields a current runtime sends with every ack (contract L7).
_CLEAN_FIELDS = {"name_hits": {}, "tombstoned": False, "unchecked_names": 0, "done_record": "written"}


def _ack(**fields) -> PurgeAck:
    return PurgeAck(**{**_CLEAN_FIELDS, **fields})


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
    await redis.set(f"{enforcing_key('bot')}:host-1", 1)  # the worker has not caught up
    # A stale aggregate claiming the worker enforces is not trusted.
    await redis.set(enforcing_key("worker"), 1)

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

    assert status == STATUS_NEEDS_REVIEW
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
                _ack(component=component, guild_id=_GUILD, outcome="purged"),
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
        await record_ack(session, request.run_id, _ack(component="bot", guild_id=_GUILD, outcome="failed"))
        await record_ack(session, request.run_id, _ack(component="worker", guild_id=_GUILD, outcome="unchanged"))
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
            session, old_run, _ack(component="bot", guild_id=_GUILD, outcome="purged")
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
            session, request.run_id, _ack(component="bot", guild_id=_GUILD, outcome="purged")
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


class _CountingAgent(_ScriptedAgent):
    def __init__(self):
        self.calls = 0

    async def run(self, user_prompt: str, *, deps: PurgeContext):
        self.calls += 1
        return await super().run(user_prompt, deps=deps)




# -- helpers for the review fixes --------------------------------------------------

_GUILDS = ("123456789012345671", "123456789012345672", "123456789012345673")


async def _seed_guild(db_session, guild_id: str, content: str = MEMORY) -> None:
    await upsert_guild_memory_blob(
        db_session,
        guild_id=guild_id,
        content=content,
        notes_consumed=1,
        model_name="stub",
        dreamed_at=_NOW - timedelta(days=1),
    )
    await db_session.commit()


async def _run(session_factory, redis, request_id, run_id, agent):
    return await run_purge(
        request_id, run_id, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=agent, sleep=_no_sleep,
    )


async def _ack_all(session_factory, run_id, guilds, details=None):
    details = details or {}
    for guild_id in guilds:
        for component in ("bot", "worker"):
            async with session_factory() as session:
                await record_ack(
                    session,
                    run_id,
                    _ack(
                        component=component,
                        guild_id=guild_id,
                        outcome="purged",
                        detail=details.get((component, guild_id), ""),
                    ),
                )
                await session.commit()


async def _check(session_factory, redis, request_id, agent):
    return await run_check(
        request_id, session_factory=session_factory, redis=redis, now=lambda: _NOW, agent=agent
    )


async def _stored(session_factory, request_id) -> ChatBotPurgeRequest:
    async with session_factory() as session:
        return await session.get(ChatBotPurgeRequest, request_id)


async def _rerun(session_factory, request_id):
    from smarter_dev.web.chat_bot_purge import start_new_run

    async with session_factory() as session:
        stored = await session.get(ChatBotPurgeRequest, request_id)
        start_new_run(stored, list_revision=1)
        await session.commit()
        return stored.run_id


class _GuildRecordingAgent(_ScriptedAgent):
    """Records which guild each model call was for (by the memory text it saw)."""

    def __init__(self, action=None, after_calls: int = 1):
        self.calls: list[str] = []
        self.action = action
        self.after_calls = after_calls

    async def run(self, user_prompt: str, *, deps: PurgeContext):
        self.calls.append(deps.memory)
        if self.action is not None and len(self.calls) == self.after_calls:
            await self.action()
        return await super().run(user_prompt, deps=deps)


def _memory_for(guild_id: str) -> str:
    return f"## People & Relationships\n- guild {guild_id[-1]} regular.\n{KAI_LINE}"


# -- item 1: a re-run purges flagged guilds again ----------------------------------


async def test_a_rerun_skips_clean_guilds_and_purges_flagged_ones_again(
    db_session, session_factory, redis
):
    for guild_id in _GUILDS:
        await _seed_guild(db_session, guild_id, _memory_for(guild_id))
    request = await _open(db_session)
    await _enforce(redis, 1)
    agent = _GuildRecordingAgent()
    await _run(session_factory, redis, request.id, request.run_id, agent)
    assert len(agent.calls) == 3
    await _ack_all(session_factory, request.run_id, _GUILDS)
    assert await _check(session_factory, redis, request.id, agent) == STATUS_COMPLETE

    # Guild 1: the bot wrote kai back into memory before its history was
    # folded; the next check finds it. Guild 3: a runtime's ack still counted
    # name hits (item J).
    async with session_factory() as session:
        memory = await get_guild_memory_blob(session, _GUILDS[0])
        memory.content = memory.content + f"\n{KAI_LINE}"
        await session.commit()
    async with session_factory() as session:
        stored = await session.get(ChatBotPurgeRequest, request.id)
        stored.steps["worker"][_GUILDS[2]]["name_hits"] = {"history": 0, "watch": 2}
        flag_modified(stored, "steps")
        await session.commit()
    assert await _check(session_factory, redis, request.id, agent) == STATUS_NEEDS_REVIEW

    rerun_id = await _rerun(session_factory, request.id)
    agent.calls.clear()
    await _run(session_factory, redis, request.id, rerun_id, agent)

    stored = await _stored(session_factory, request.id)
    memory_steps = stored.steps["memory"]
    assert memory_steps[_GUILDS[1]].get("earlier_run") is True  # clean: left alone
    assert not memory_steps[_GUILDS[0]].get("earlier_run")
    assert not memory_steps[_GUILDS[2]].get("earlier_run")
    # Guild 1 went back through the agent; guild 3 had nothing to show it.
    assert len(agent.calls) == 1 and "guild 1 regular" in agent.calls[0]
    async with session_factory() as session:
        memory = await get_guild_memory_blob(session, _GUILDS[0])
        assert KAI_LINE not in memory.content


async def test_a_guild_with_an_unresolved_item_is_purged_again(db_session, session_factory, redis):
    from smarter_dev.web.chat_bot_purge import flagged_guilds

    steps = {
        "memory": {_GUILDS[0]: {"outcome": "purged", "unresolved": [{"location": "memory", "reason": "x"}]}},
        "final_notes": {_GUILDS[1]: {"outcome": "purged", "unresolved": [{"location": "note:1", "reason": "x"}]}},
        "bot": {_GUILDS[2]: {**_CLEAN_FIELDS, "outcome": "purged", "name_hits": {"history": 1}}},
    }
    report = {
        "remains": [
            {"store": "chat_agent_memory_notes", "guild_id": "999999999999999999"},
            {"store": "redis", "location": "chat_agent:1:history"},
        ]
    }
    assert flagged_guilds(steps, report) == {*_GUILDS, "999999999999999999"}


def test_flags_come_from_the_structured_fields_not_the_detail():
    from smarter_dev.web.chat_bot_purge import possible_remains
    from smarter_dev.web.chat_bot_purge import run_outcome

    clean = {**_CLEAN_FIELDS, "outcome": "purged"}
    steps = {
        "guild_ids": [_GUILD],
        "memory": {_GUILD: {"outcome": "purged"}},
        "bot": {_GUILD: {**clean, "name_hits": {"history": 0, "watch": 2}}},
        # Free text that once reopened requests decides nothing now.
        "worker": {_GUILD: {**clean, "detail": "NAME_HITS=3 Tombstoned=1 history_name_hits = 3"}},
    }
    assert possible_remains(steps) == [
        {"guild_id": _GUILD, "component": "bot", "reasons": ["2 name hit(s)"]}
    ]
    assert run_outcome(steps, {"remains": []}) == STATUS_NEEDS_REVIEW
    steps["bot"][_GUILD]["name_hits"] = {"history": 0}
    assert run_outcome(steps, {"remains": []}) == STATUS_COMPLETE
    # An older runtime's ack without the fields is accepted but flagged.
    steps["worker"][_GUILD] = {"outcome": "purged", "stores": [], "detail": ""}
    assert possible_remains(steps)[0]["reasons"] == ["missing structured fields"]
    assert run_outcome(steps, {"remains": []}) == STATUS_NEEDS_REVIEW


# -- item 1 (ordering): the final notes pass ---------------------------------------


async def test_a_note_written_from_unpurged_history_is_caught_by_the_final_pass(
    db_session, session_factory, redis
):
    from smarter_dev.web.crud import create_memory_note
    from smarter_dev.web.models import ChatAgentMemoryNote

    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    agent = _ScriptedAgent()
    await _run(session_factory, redis, request.id, request.run_id, agent)
    # Before the bot folds its history it notes kai again.
    async with session_factory() as session:
        await create_memory_note(
            session,
            guild_id=_GUILD,
            channel_id="555000111222333444",
            content="kai is back on embedded rust.",
            created_at=_NOW + timedelta(minutes=1),
            day_start=_NOW,
        )
        await session.commit()

    await _ack_all(session_factory, request.run_id, [_GUILD])
    status = await _check(session_factory, redis, request.id, _ScriptedAgent())

    assert status == STATUS_COMPLETE
    stored = await _stored(session_factory, request.id)
    assert stored.steps["final_notes"][_GUILD]["notes_dropped"] == 1
    async with session_factory() as session:
        assert not (await session.scalars(select(ChatAgentMemoryNote))).all()


# -- item 3: the check decodes JSON ------------------------------------------------


async def test_the_check_finds_names_after_escapes_and_ascii_escaped_names(
    db_session, session_factory, redis
):
    from smarter_dev.shared.privacy_purge import PurgeTarget
    from smarter_dev.web.chat_bot_purge import scan_stores
    from smarter_dev.web.models import ProactiveAgentHistory

    target = PurgeTarget.build(_KAI_ID, ["Alice", "Zoë"])
    line_start = json.dumps([{"content": "hello\nAlice said hi"}])
    ascii_escaped = json.dumps({"text": "Zoë was here"}, ensure_ascii=True)
    assert "\\nAlice" in line_start and "\\u00eb" in ascii_escaped
    # The old raw search over serialized JSON missed both.
    assert target.name_hits(line_start) == 0 and target.name_hits(ascii_escaped) == 0

    await redis.set("chat_agent:999000111222333444:history", line_start)
    await redis.rpush("proactive:v1:{guild:1}:history", ascii_escaped)
    await redis.set("chat_agent:999000111222333445:topic", "plain text about Alice")
    db_session.add(
        ProactiveAgentHistory(
            guild_id=_GUILD, schema_version=1, revision=1, checksum="x",
            history=[{"parts": [{"content": "line one\nZoë again"}]}],
        )
    )
    await db_session.commit()

    report = await scan_stores(db_session, redis, target)
    found = {hit["location"]: hit["name_hits"] for hit in report["remains"]}
    assert found == {
        "chat_agent:999000111222333444:history": 1,
        "proactive:v1:{guild:1}:history": 1,
        "chat_agent:999000111222333445:topic": 1,
        f"guild:{_GUILD}": 1,
    }


# -- item 4: no orphaned stream commands -------------------------------------------


async def _orphan(redis, request, run_id) -> None:
    command = PurgeCommand(
        schema_version=1, request_id=request.id, run_id=run_id, user_id=_KAI_ID,
        names=["kai"], guild_ids=[_GUILD], created_at=_NOW,
    )
    # XADD landed, the job died before recording the entry ID.
    await redis.xadd(PURGE_STREAM, {"payload": command.model_dump_json()})


async def test_closing_deletes_a_command_whose_entry_id_was_never_recorded(
    db_session, session_factory, redis
):
    request = await _open(db_session)
    await _orphan(redis, request, request.run_id)
    await redis.xadd(PURGE_STREAM, {"payload": json.dumps({"request_id": "other", "run_id": "x"})})

    async with session_factory() as session:
        await close_request(session, request.id, now=_NOW, redis=redis)
        await session.commit()

    entries = await redis.xrange(PURGE_STREAM)
    assert len(entries) == 1 and b"other" in entries[0][1][b"payload"]


async def test_a_new_run_deletes_the_previous_runs_orphan(db_session, session_factory, redis):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _orphan(redis, request, request.run_id)
    rerun_id = await _rerun(session_factory, request.id)

    await _run(session_factory, redis, request.id, rerun_id, _ScriptedAgent())

    runs = [
        PurgeCommand.model_validate_json(fields[b"payload"]).run_id
        for _, fields in await redis.xrange(PURGE_STREAM)
    ]
    assert runs == [rerun_id]


async def test_an_ack_for_an_unknown_run_deletes_its_command(redis):
    import uuid

    from smarter_dev.web.chat_bot_purge import delete_commands

    run_id = uuid.uuid4()
    await redis.xadd(
        PURGE_STREAM,
        {"payload": json.dumps({"request_id": str(uuid.uuid4()), "run_id": str(run_id)})},
    )
    assert await delete_commands(redis, run_id=run_id) == 1
    assert await redis.xlen(PURGE_STREAM) == 0


async def test_the_command_stream_is_trimmed_by_age(db_session, session_factory, redis):
    await _seed_memory(db_session)
    old_ms = int((_NOW - timedelta(days=8)).timestamp() * 1000)
    await redis.xadd(PURGE_STREAM, {"payload": "{}"}, id=f"{old_ms}-0")
    request = await _open(db_session)
    await _enforce(redis, 1)

    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())

    entries = await redis.xrange(PURGE_STREAM)
    assert len(entries) == 1
    assert PurgeCommand.model_validate_json(entries[0][1][b"payload"]).run_id == request.run_id


async def test_the_intent_is_recorded_before_the_command_is_sent(
    db_session, session_factory, redis, monkeypatch
):
    from smarter_dev.web import chat_bot_purge

    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    seen = []

    async def dying_send(redis_client, command, *, now):
        stored = await _stored(session_factory, request.id)
        seen.append(stored.steps.get("command_intent"))
        await redis_client.xadd(PURGE_STREAM, {"payload": command.model_dump_json()})
        raise RuntimeError("worker died")

    monkeypatch.setattr(chat_bot_purge, "send_command", dying_send)
    with pytest.raises(RuntimeError):
        await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())

    assert seen == [str(request.run_id)]
    async with session_factory() as session:
        await close_request(session, request.id, now=_NOW, redis=redis)
        await session.commit()
    assert await redis.xlen(PURGE_STREAM) == 0


# -- item 6: close or resubmit stops a run between guilds ---------------------------


@pytest.mark.parametrize("action", ["close", "resubmit"])
async def test_stopping_mid_run_leaves_the_remaining_guilds_untouched(
    db_session, session_factory, redis, action
):
    for guild_id in _GUILDS:
        await _seed_guild(db_session, guild_id, _memory_for(guild_id))
    request = await _open(db_session)
    await _enforce(redis, 1)

    async def stop():
        async with session_factory() as session:
            if action == "close":
                await close_request(session, request.id, now=_NOW, redis=redis)
            else:
                await open_purge_request(
                    session, discord_user_id=_KAI_ID, names=["kai"], requested_by="admin-1"
                )
            await session.commit()

    agent = _GuildRecordingAgent(stop, after_calls=1)
    status = await _run(session_factory, redis, request.id, request.run_id, agent)

    assert status == "superseded"
    assert len(agent.calls) == 1  # guilds 2 and 3 never reached the model
    for guild_id in _GUILDS:
        async with session_factory() as session:
            memory = await get_guild_memory_blob(session, guild_id)
            assert memory.content == _memory_for(guild_id)  # guild 1's write refused too
    assert await redis.xlen(PURGE_STREAM) == 0


# -- item 10: one open request per user, races handled -----------------------------


async def test_losing_the_race_to_open_a_request_reuses_the_winners(
    db_session, session_factory, monkeypatch
):
    from smarter_dev.web import chat_bot_purge

    first = await _open(db_session)
    real = chat_bot_purge._open_request_for
    calls = []

    async def miss_once(session, user_id):
        calls.append(1)
        if len(calls) == 1:
            return None  # the other admin's insert is not visible yet
        return await real(session, user_id)

    monkeypatch.setattr(chat_bot_purge, "_open_request_for", miss_once)
    async with session_factory() as session:
        second = await open_purge_request(
            session, discord_user_id=_KAI_ID, names=["kai"], requested_by="admin-2"
        )
        await session.commit()

    assert second.id == first.id
    async with session_factory() as session:
        rows = (await session.scalars(select(ChatBotPurgeRequest))).all()
    assert len(rows) == 1


async def test_the_block_list_inserts_never_collide(db_session):
    from smarter_dev.web.chat_bot_purge import add_blocked_user
    from smarter_dev.web.models import ChatBotBlockedUsersRevision

    # A concurrent first block already created the revision row.
    db_session.add(ChatBotBlockedUsersRevision(id=1, revision=4))
    await db_session.commit()
    assert await add_blocked_user(db_session, _KAI_ID) == 5
    # Someone else blocked the same user in between: no error, no bump.
    assert await add_blocked_user(db_session, _KAI_ID) == 5
    await db_session.commit()
    assert await add_blocked_user(db_session, "1234567890123456789012") == 6


# -- item 11: the command is validated before memory is touched ---------------------


async def test_an_invalid_command_fails_the_run_before_any_memory_changes(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    await _seed_guild(db_session, "not-a-snowflake", MEMORY)
    request = await _open(db_session)
    await _enforce(redis, 1)
    agent = _CountingAgent()

    status = await _run(session_factory, redis, request.id, request.run_id, agent)

    assert status == STATUS_NEEDS_REVIEW
    assert agent.calls == 0
    assert "not Discord snowflakes" in (await _stored(session_factory, request.id)).steps["error"]
    async with session_factory() as session:
        assert (await get_guild_memory_blob(session, _GUILD)).content == MEMORY
    assert await redis.xlen(PURGE_STREAM) == 0


# -- items 12, K, L: runtime status from per-process keys ---------------------------


async def test_runtime_status_takes_the_min_over_live_processes_and_ignores_the_aggregate(redis):
    from smarter_dev.web.chat_bot_purge import runtime_status
    from smarter_dev.web.chat_bot_purge import start_refusal

    await redis.set(enforcing_key("bot"), 9)  # stale aggregate
    await redis.set(f"{enforcing_key('bot')}:a-1", 3, ex=180)
    await redis.set(f"{enforcing_key('bot')}:b-2", 2, ex=180)
    await redis.set(f"{enforcing_key('worker')}:c-3", 3, ex=180)
    await redis.set("privacy:v1:consumer:bot:a-1", "1", ex=180)

    status = await runtime_status(redis)
    assert status["bot"] == {"processes": 2, "revision": 2, "consumers": 1}
    assert status["worker"] == {"processes": 1, "revision": 3, "consumers": 0}
    assert "worker" in start_refusal(status)

    await redis.set("privacy:v1:consumer:worker:c-3", "1", ex=180)
    assert start_refusal(await runtime_status(redis)) is None
    await redis.delete(f"{enforcing_key('worker')}:c-3")
    status = await runtime_status(redis)
    assert status["worker"]["revision"] is None
    assert "Not enforcing" in start_refusal(status)


# -- item 13: check coverage -------------------------------------------------------


async def test_the_check_reports_audit_tables_and_extra_keys_as_information(
    db_session, redis
):
    import uuid

    from smarter_dev.shared.privacy_purge import PurgeTarget
    from smarter_dev.web.chat_bot_purge import scan_stores
    from smarter_dev.web.models import ChatAgentEngagement

    db_session.add(
        ChatAgentEngagement(
            id=uuid.uuid4(), guild_id=_GUILD, channel_id="1", activation_user_id=_KAI_ID,
            activation_username="kai", activation_message_id="2", started_at=_NOW,
        )
    )
    await db_session.commit()
    await redis.set("proactive:v1:{guild:1}:pending-dropped", _KAI_ID)
    await redis.xadd("proactive:v1:control", {"payload": json.dumps({"note": "kai"})})

    report = await scan_stores(db_session, redis, PurgeTarget.build(_KAI_ID, ["kai"]))

    assert [hit["store"] for hit in report["information"]] == ["chat_agent_engagements"]
    assert report["information"][0]["id_hits"] == 1
    assert {hit["location"] for hit in report["operational"]} == {
        "proactive:v1:{guild:1}:pending-dropped",
        "proactive:v1:control",
    }
    assert report["remains"] == []


async def test_streams_are_read_past_the_old_5000_entry_cap(db_session, redis):
    from smarter_dev.shared.privacy_purge import PurgeTarget
    from smarter_dev.web.chat_bot_purge import scan_stores

    async with redis.pipeline() as pipe:
        for i in range(5200):
            pipe.xadd("proactive:v1:dead-letter", {"payload": f"entry {i}"})
        await pipe.execute()
    await redis.xadd("proactive:v1:dead-letter", {"payload": f"kai {_KAI_ID}"})

    report = await scan_stores(db_session, redis, PurgeTarget.build(_KAI_ID, ["kai"]))

    assert [hit["location"] for hit in report["operational"]] == ["proactive:v1:dead-letter"]


# -- answer B: a second execution of the same run does nothing ---------------------


async def test_a_reclaimed_second_execution_of_a_running_purge_does_nothing(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    second: list[str] = []
    inner = _CountingAgent()

    async def run_again():
        second.append(await _run(session_factory, redis, request.id, request.run_id, inner))

    status = await _run(
        session_factory, redis, request.id, request.run_id, _InterruptingAgent(run_again)
    )

    assert second == ["already_running"]
    assert inner.calls == 0
    assert status == STATUS_AWAITING_ACKS
    assert await redis.xlen(PURGE_STREAM) == 1


@pytest.mark.parametrize("action", ["close", "resubmit"])
async def test_stopping_after_guild_one_commits_leaves_guilds_two_on_untouched(
    db_session, session_factory, redis, monkeypatch, action
):
    from smarter_dev.web import chat_memory_purge

    for guild_id in _GUILDS:
        await _seed_guild(db_session, guild_id, _memory_for(guild_id))
    request = await _open(db_session)
    await _enforce(redis, 1)
    real = chat_memory_purge.purge_guild_memory

    async def purge_then_stop(*args, **kwargs):
        result = await real(*args, **kwargs)
        async with session_factory() as session:
            if action == "close":
                await close_request(session, request.id, now=_NOW, redis=redis)
            else:
                await open_purge_request(
                    session, discord_user_id=_KAI_ID, names=["kai"], requested_by="admin-1"
                )
            await session.commit()
        return result

    monkeypatch.setattr(chat_memory_purge, "purge_guild_memory", purge_then_stop)
    agent = _GuildRecordingAgent()
    status = await _run(session_factory, redis, request.id, request.run_id, agent)

    assert status == "superseded"
    assert len(agent.calls) == 1
    async with session_factory() as session:
        first = await get_guild_memory_blob(session, _GUILDS[0])
        assert KAI_LINE not in first.content  # guild 1 finished before the stop
        for guild_id in _GUILDS[1:]:
            memory = await get_guild_memory_blob(session, guild_id)
            assert memory.content == _memory_for(guild_id)
    assert await redis.xlen(PURGE_STREAM) == 0


async def test_a_second_execution_after_the_run_finished_does_nothing(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    assert await _run(
        session_factory, redis, request.id, request.run_id, _ScriptedAgent()
    ) == STATUS_AWAITING_ACKS
    agent = _CountingAgent()

    assert await _run(session_factory, redis, request.id, request.run_id, agent) == "already_running"
    assert agent.calls == 0
    assert await redis.xlen(PURGE_STREAM) == 1
    assert (await _stored(session_factory, request.id)).status == STATUS_AWAITING_ACKS


# == second review round ===========================================================


# -- a stale runtime: the run waits, then ends in review with the reason ------------


async def test_a_process_holding_an_old_list_revision_holds_the_purge_back(
    db_session, session_factory, redis
):
    from smarter_dev.web.chat_bot_purge import runtime_status
    from smarter_dev.web.chat_bot_purge import start_refusal

    await _seed_memory(db_session)
    request = await _open(db_session)  # the list is now at revision 1
    for component in ("bot", "worker"):
        await redis.set(f"privacy:v1:consumer:{component}:host-1", "1", ex=180)
    await redis.set(f"{enforcing_key('worker')}:host-1", 1, ex=180)
    # One bot process kept running through a list outage on revision 0.
    await redis.set(f"{enforcing_key('bot')}:host-1", 0, ex=180)
    await redis.set(f"{enforcing_key('bot')}:host-2", 1, ex=180)

    # The admin start is not refused (it only needs every component reporting
    # some revision and a consumer); the run is what waits.
    assert start_refusal(await runtime_status(redis)) is None

    polls: list[int] = []

    async def poll(_seconds):
        polls.append(1)
        if len(polls) == 2:
            stored = await _stored(session_factory, request.id)
            assert stored.status == STATUS_WAITING
            assert await redis.xlen(PURGE_STREAM) == 0
            await redis.set(f"{enforcing_key('bot')}:host-1", 1, ex=180)

    agent = _CountingAgent()
    status = await run_purge(
        request.id, request.run_id, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=agent, sleep=poll, wait_seconds=60, poll_seconds=5,
    )
    assert status == STATUS_AWAITING_ACKS
    assert len(polls) == 2 and agent.calls == 1


async def test_a_wait_that_times_out_ends_in_review_with_the_reason(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await redis.set(f"{enforcing_key('bot')}:host-2", 0, ex=180)  # stuck on its last list
    agent = _CountingAgent()

    status = await run_purge(
        request.id, request.run_id, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=agent, sleep=_no_sleep, wait_seconds=10, poll_seconds=5,
    )

    assert status == STATUS_NEEDS_REVIEW and agent.calls == 0
    stored = await _stored(session_factory, request.id)
    assert "bot holds revision 0" in stored.steps["error"]
    assert "revision 1" in stored.steps["error"]
    assert await redis.xlen(PURGE_STREAM) == 0
    async with session_factory() as session:
        assert (await get_guild_memory_blob(session, _GUILD)).content == MEMORY


# -- M2: an unchecked name keeps the run in review ----------------------------------


async def test_an_unchecked_name_ends_in_review(db_session, session_factory, redis):
    await _seed_memory(db_session)
    request = await _open(db_session, names=("kai", "k"))
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    await _ack_all(session_factory, request.run_id, [_GUILD])

    assert await _check(session_factory, redis, request.id, _ScriptedAgent()) == STATUS_NEEDS_REVIEW
    stored = await _stored(session_factory, request.id)
    assert stored.check_report["unchecked_names"] == ["k"]


# -- M3: pydantic-ai histories, JSON inside JSON ---------------------------------------


async def test_the_check_reads_tool_args_and_returns_in_a_real_history(db_session, redis):
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.messages import ToolReturnPart

    from smarter_dev.shared.privacy_purge import PurgeTarget
    from smarter_dev.web.chat_bot_purge import scan_stores
    from smarter_dev.web.models import ChatAgentTurn  # noqa: F401 — table exists
    from smarter_dev.web.models import ProactiveAgentHistory

    target = PurgeTarget.build(_KAI_ID, ["Zoë"])
    messages = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="note", args=json.dumps({"text": "line\nZoë"}), tool_call_id="c1")]
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="note", content=json.dumps({"saved": "ok\nZoë"}), tool_call_id="c1")]
        ),
    ]
    dumped = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    raw = json.dumps(dumped)
    assert "Zo\\\\u00eb" in raw  # escaped twice: once in args, once in the dump
    db_session.add(
        ProactiveAgentHistory(guild_id=_GUILD, schema_version=1, revision=1, checksum="x", history=dumped)
    )
    await db_session.commit()
    await redis.set("proactive:v1:{guild:1}:history", raw)

    report = await scan_stores(db_session, redis, target)

    found = {hit["location"]: hit["name_hits"] for hit in report["remains"]}
    assert found == {f"guild:{_GUILD}": 2, "proactive:v1:{guild:1}:history": 2}


# -- M4: notes written between runs ---------------------------------------------------


async def test_a_note_written_after_an_earlier_runs_memory_step_is_still_reviewed(
    db_session, session_factory, redis
):
    from smarter_dev.web.crud import create_memory_note

    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    clock = {"now": _NOW}

    def now():
        return clock["now"]

    # Run 1 purges the guild, then stalls (no acks ever arrive).
    await run_purge(
        request.id, request.run_id, session_factory=session_factory, redis=redis,
        now=now, agent=_ScriptedAgent(), sleep=_no_sleep,
    )
    clock["now"] = _NOW + timedelta(hours=1)
    async with session_factory() as session:
        await create_memory_note(
            session, guild_id=_GUILD, channel_id="555000111222333444",
            content="the rust person is moving to embedded work.",
            created_at=_NOW + timedelta(minutes=30), day_start=_NOW,
        )
        await session.commit()
    clock["now"] = _NOW + timedelta(hours=2)
    rerun_id = await _rerun(session_factory, request.id)
    await run_purge(
        request.id, rerun_id, session_factory=session_factory, redis=redis,
        now=now, agent=_ScriptedAgent(), sleep=_no_sleep,
    )
    stored = await _stored(session_factory, request.id)
    assert stored.steps["memory"][_GUILD]["earlier_run"] is True
    await _ack_all(session_factory, rerun_id, [_GUILD])

    seen: list[str] = []

    class _Watch(_ScriptedAgent):
        async def run(self, user_prompt, *, deps):
            seen.extend(content for _, content in deps.notes)
            return await super().run(user_prompt, deps=deps)

    await run_check(request.id, session_factory=session_factory, redis=redis, now=now, agent=_Watch())
    assert "the rust person is moving to embedded work." in seen


# -- M5: any searched-store hit ends in review; the scan-only check ------------------


async def test_an_operational_or_audit_hit_keeps_the_request_out_of_complete(
    db_session, session_factory, redis
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    await _ack_all(session_factory, request.run_id, [_GUILD])
    await redis.xadd("proactive:v1:dead-letter", {"payload": f"from {_KAI_ID}"})

    assert await _check(session_factory, redis, request.id, _ScriptedAgent()) == STATUS_NEEDS_REVIEW
    stored = await _stored(session_factory, request.id)
    assert stored.check_report["hit_counts"] == {"redis": 1}

    await redis.delete("proactive:v1:dead-letter")
    agent = _CountingAgent()
    status = await run_check(
        request.id, session_factory=session_factory, redis=redis, now=lambda: _NOW,
        agent=agent, scan_only=True,
    )
    assert status == STATUS_COMPLETE and agent.calls == 0


async def test_the_scan_only_check_never_runs_the_notes_pass(db_session, session_factory, redis):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    await _ack_all(session_factory, request.run_id, [_GUILD])  # status: checking
    agent = _CountingAgent()

    status = await run_check(
        request.id, session_factory=session_factory, redis=redis, now=lambda: _NOW,
        agent=agent, scan_only=True,
    )

    assert status == STATUS_CHECKING and agent.calls == 0
    assert (await _stored(session_factory, request.id)).steps["final_notes"] == {}


@pytest.mark.parametrize(
    "ack",
    [
        {"outcome": "failed"},
        {"outcome": "purged", "name_hits": {"history": 3}},
        {"outcome": "purged", "tombstoned": True},
        {"outcome": "purged", "unchecked_names": 1},
        {"outcome": "purged", "done_record": None},
    ],
    ids=["failed", "name-hits", "tombstoned", "unchecked", "missing-fields"],
)
async def test_a_late_flagged_ack_reopens_a_complete_request(
    db_session, session_factory, redis, ack
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    await _ack_all(session_factory, request.run_id, [_GUILD])
    assert await _check(session_factory, redis, request.id, _ScriptedAgent()) == STATUS_COMPLETE

    async with session_factory() as session:
        await record_ack(
            session,
            request.run_id,
            PurgeAck(
                **{
                    k: v
                    for k, v in {**_CLEAN_FIELDS, "component": "worker", "guild_id": _GUILD, **ack}.items()
                    if v is not None
                }
            ),
        )
        await session.commit()

    stored = await _stored(session_factory, request.id)
    assert stored.status == STATUS_NEEDS_REVIEW
    # The structured fields are stored as sent; the page and run_outcome read them.
    for field, value in ack.items():
        assert stored.steps["worker"][_GUILD].get(field) == value


# -- tombstones ---------------------------------------------------------------------


async def test_a_tombstoned_guild_blocks_close_keeps_the_command_and_ends_in_review(
    db_session, session_factory, redis
):
    from smarter_dev.shared.privacy_purge import history_tombstone_key
    from smarter_dev.web.chat_bot_purge import CloseRefused

    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    await redis.set(
        history_tombstone_key(_GUILD),
        json.dumps({"run_id": str(request.run_id), "request_id": str(request.id)}),
    )
    await _ack_all(session_factory, request.run_id, [_GUILD])

    assert await _check(session_factory, redis, request.id, _ScriptedAgent()) == STATUS_NEEDS_REVIEW
    stored = await _stored(session_factory, request.id)
    assert stored.check_report["tombstoned"] == [_GUILD]
    assert await redis.xlen(PURGE_STREAM) == 1  # the worker may still retry it
    async with session_factory() as session:
        with pytest.raises(CloseRefused):
            await close_request(session, request.id, now=_NOW, redis=redis)
        await session.rollback()
    assert await redis.xlen(PURGE_STREAM) == 1
    assert (await _stored(session_factory, request.id)).discord_user_id == _KAI_ID

    # Another request's tombstone does not hold this one; an old plain one does.
    await redis.set(history_tombstone_key(_GUILD), json.dumps({"request_id": "other"}))
    async with session_factory() as session:
        await close_request(session, request.id, now=_NOW, redis=redis)
        await session.commit()
    assert await redis.xlen(PURGE_STREAM) == 0


async def test_a_plain_tombstone_counts_as_unknown_run(redis):
    import uuid

    from smarter_dev.shared.privacy_purge import history_tombstone_key
    from smarter_dev.web.chat_bot_purge import tombstoned_guilds

    await redis.set(history_tombstone_key(_GUILD), "1")
    assert await tombstoned_guilds(redis, uuid.uuid4(), [_GUILD, _GUILDS[0]]) == [_GUILD]


async def test_a_tombstone_with_extra_keys_is_still_attributed_to_its_request(redis):
    import uuid

    from smarter_dev.shared.privacy_purge import history_tombstone_key
    from smarter_dev.web.chat_bot_purge import tombstoned_guilds

    mine, other = uuid.uuid4(), uuid.uuid4()
    await redis.set(
        history_tombstone_key(_GUILD),
        json.dumps({"run_id": str(uuid.uuid4()), "request_id": str(mine), "revision": 7}),
    )
    assert await tombstoned_guilds(redis, mine, [_GUILD]) == [_GUILD]
    assert await tombstoned_guilds(redis, other, [_GUILD]) == []


def test_tombstoned_acks_are_flagged():
    from smarter_dev.web.chat_bot_purge import ack_flagged
    from smarter_dev.web.chat_bot_purge import flagged_guilds

    assert ack_flagged({**_CLEAN_FIELDS, "outcome": "purged", "tombstoned": True})
    assert not ack_flagged({**_CLEAN_FIELDS, "outcome": "purged"})
    steps = {"worker": {_GUILD: {**_CLEAN_FIELDS, "outcome": "purged", "tombstoned": True}}}
    assert flagged_guilds(steps, {"tombstoned": [_GUILDS[0]]}) == {_GUILD, _GUILDS[0]}


# -- L1: a re-executed run reuses its command ----------------------------------------


async def test_a_reexecution_after_xadd_reuses_the_entry(db_session, session_factory, redis):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    # The first execution died right after XADD: status and lease as it left them.
    async with session_factory() as session:
        stored = await session.get(ChatBotPurgeRequest, request.id)
        stored.status = "purging"
        stored.steps["command_entry_id"] = None
        flag_modified(stored, "steps")
        await session.commit()

    status = await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())

    assert status == STATUS_AWAITING_ACKS
    entries = await redis.xrange(PURGE_STREAM)
    assert len(entries) == 1
    assert (await _stored(session_factory, request.id)).steps["command_entry_id"] == entries[0][0].decode()


# -- L3: trimming without an XADD; retrying the post-settle delete -------------------


async def test_the_stream_is_trimmed_while_a_run_waits(db_session, session_factory, redis):
    old_ms = int((_NOW - timedelta(days=8)).timestamp() * 1000)
    await redis.xadd(PURGE_STREAM, {"payload": "{}"}, id=f"{old_ms}-0")
    request = await _open(db_session)
    await run_purge(
        request.id, request.run_id, session_factory=session_factory, redis=redis,
        now=lambda: _NOW, agent=_ScriptedAgent(), sleep=_no_sleep, wait_seconds=5,
    )
    assert await redis.xlen(PURGE_STREAM) == 0


async def test_the_post_settle_delete_is_retried(redis, monkeypatch):
    import uuid

    from smarter_dev.web import chat_bot_purge

    calls = []
    real = chat_bot_purge.delete_commands

    async def flaky(redis_client, **match):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("redis blinked")
        return await real(redis_client, **match)

    monkeypatch.setattr(chat_bot_purge, "delete_commands", flaky)
    request_id = uuid.uuid4()
    await redis.xadd(PURGE_STREAM, {"payload": json.dumps({"request_id": str(request_id)})})

    assert await chat_bot_purge.delete_commands_retrying(
        redis, request_id=request_id, sleep=_no_sleep
    )
    assert len(calls) == 3 and await redis.xlen(PURGE_STREAM) == 0


# -- L5: a job exception leaves the request failed ------------------------------------


async def test_a_job_exception_leaves_the_request_failed_and_rerunnable(
    db_session, session_factory, redis
):
    from smarter_dev.web.chat_bot_purge import STATUS_FAILED
    from smarter_dev.web.chat_bot_purge import mark_failed

    request = await _open(db_session)
    assert await mark_failed(session_factory, request.id, None, "OperationalError")
    stored = await _stored(session_factory, request.id)
    assert stored.status == STATUS_FAILED
    assert "OperationalError" in stored.steps["error"]
    rerun_id = await _rerun(session_factory, request.id)
    assert (await _stored(session_factory, request.id)).run_id == rerun_id


# -- L7: one SCAN per status read ------------------------------------------------------


async def test_runtime_status_scans_once(redis, monkeypatch):
    from smarter_dev.web.chat_bot_purge import runtime_status

    await _enforce(redis, 1)
    patterns = []
    real = redis.scan_iter

    def counting(*args, **kwargs):
        patterns.append(kwargs.get("match"))
        return real(*args, **kwargs)

    monkeypatch.setattr(redis, "scan_iter", counting)
    await runtime_status(redis)
    assert patterns == ["privacy:v1:*"]


# -- item 11: the real reason -------------------------------------------------------


def test_a_command_that_cannot_be_built_says_why():
    from smarter_dev.web.chat_bot_purge import command_problem

    assert "not Discord snowflakes" in command_problem(["123", _GUILD])
    many = [str(10**17 + i) for i in range(501)]
    assert "501 guilds" in command_problem(many)
    assert command_problem([_GUILD]) is None


# -- ack timeout ----------------------------------------------------------------------


async def test_acks_missing_for_an_hour_end_in_review_and_late_acks_still_count(
    db_session, session_factory, redis
):
    from smarter_dev.web.chat_bot_purge import expire_acks
    from smarter_dev.web.chat_bot_purge import missing_acks

    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    async with session_factory() as session:
        await record_ack(
            session,
            request.run_id,
            _ack(component="bot", guild_id=_GUILD, outcome="purged"),
            now=_NOW + timedelta(minutes=10),
        )
        await session.commit()
    stored = await _stored(session_factory, request.id)
    last = datetime.fromisoformat(stored.steps["last_ack_at"])
    assert last == _NOW + timedelta(minutes=10)

    async with session_factory() as session:
        assert not await expire_acks(session, request.id, now=last + timedelta(minutes=59))
    async with session_factory() as session:
        assert await expire_acks(session, request.id, now=last + timedelta(minutes=61))

    stored = await _stored(session_factory, request.id)
    assert stored.status == STATUS_NEEDS_REVIEW
    assert stored.steps["worker"][_GUILD]["outcome"] == "failed"
    assert stored.steps["worker"][_GUILD]["detail"] == "no ack within 1 hour"
    assert missing_acks(stored.steps) == [("worker", _GUILD)]
    assert await redis.xlen(PURGE_STREAM) == 1  # kept for the late runtime

    # The late real ack replaces the stand-in and the run goes on to its check.
    async with session_factory() as session:
        late = await record_ack(
            session, request.run_id, _ack(component="worker", guild_id=_GUILD, outcome="purged")
        )
        await session.commit()
    assert late.status == STATUS_CHECKING
    assert await _check(session_factory, redis, request.id, _ScriptedAgent()) == STATUS_COMPLETE


async def test_the_page_view_applies_the_ack_timeout(db_session, session_factory, redis):
    from unittest.mock import AsyncMock
    from unittest.mock import patch

    from smarter_dev.web.bot_admin.privacy_purge import PrivacyPurgeAdminController

    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    await _run(session_factory, redis, request.id, request.run_id, _ScriptedAgent())
    async with session_factory() as session:
        stored = await session.get(ChatBotPurgeRequest, request.id)
        stored.steps["command_sent_at"] = "2020-01-01T00:00:00+00:00"
        flag_modified(stored, "steps")
        await session.commit()

    module = "smarter_dev.web.bot_admin.privacy_purge"
    with (
        patch(f"{module}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{module}.get_flash_messages", return_value=[]),
        patch(f"{module}.get_redis_client", return_value=redis),
    ):
        response = await PrivacyPurgeAdminController.view.fn(
            None, request=object(), db_session=db_session, request_id=request.id
        )
    assert response.context["purge"].status == STATUS_NEEDS_REVIEW
    assert response.context["missing_acks"] == [("bot", _GUILD), ("worker", _GUILD)]
    # "Run the purge again" works from here.
    rerun_id = await _rerun(session_factory, request.id)
    assert (await _stored(session_factory, request.id)).status == "queued" and rerun_id


# -- names with JSON-special characters, in every store the check reads ---------------


@pytest.mark.parametrize(
    "name",
    ['Kai "the Rustacean"', "back\\slash", "tab\there", "new\nline"],
    ids=["quote", "backslash", "tab", "newline"],
)
async def test_the_check_finds_special_character_names_in_serialised_stores(
    db_session, redis, name
):
    import uuid

    from smarter_dev.shared.privacy_purge import PurgeTarget
    from smarter_dev.web.chat_bot_purge import scan_stores
    from smarter_dev.web.models import ChatAgentEngagement
    from smarter_dev.web.models import ChatAgentError
    from smarter_dev.web.models import ProactiveAgentHistory

    target = PurgeTarget.build(_KAI_ID, [name])
    text = f"later {name} asked again"
    history = [{"parts": [{"content": text}]}]
    await redis.set("chat_agent:999000111222333444:history", json.dumps(history))
    await redis.rpush("proactive:v1:{guild:1}:history", json.dumps({"m": text}, ensure_ascii=True))
    await redis.hset("chat_agent:999000111222333445:notes", "k", json.dumps([text]))
    await redis.xadd("proactive:v1:dead-letter", {"payload": json.dumps({"body": text})})
    await redis.set("chat_agent:999000111222333446:topic", text)  # plain text, searched raw
    db_session.add(
        ProactiveAgentHistory(
            guild_id=_GUILD, schema_version=1, revision=1, checksum="x", history=history
        )
    )
    engagement = ChatAgentEngagement(
        id=uuid.uuid4(), guild_id=_GUILD, channel_id="1", activation_user_id="3",
        activation_username="x", activation_message_id="2", started_at=_NOW,
    )
    db_session.add(engagement)
    db_session.add(
        ChatAgentError(
            id=uuid.uuid4(), engagement_id=engagement.id, request_id="r1", guild_id=_GUILD,
            channel_id="1", error_type="X", error_message="boom", traceback="",
            provider_body=json.dumps({"input": text}), error_context={"last": text},
            occurred_at=_NOW,
        )
    )
    await db_session.commit()

    report = await scan_stores(db_session, redis, target)

    remains = {hit["location"] for hit in report["remains"]}
    assert remains == {
        "chat_agent:999000111222333444:history",
        "proactive:v1:{guild:1}:history",
        "chat_agent:999000111222333445:notes",
        "chat_agent:999000111222333446:topic",
        f"guild:{_GUILD}",
    }
    assert [hit["location"] for hit in report["operational"]] == ["proactive:v1:dead-letter"]
    errors = [hit for hit in report["information"] if hit["store"] == "chat_agent_errors"]
    assert len(errors) == 1 and errors[0]["name_hits"] == 2
