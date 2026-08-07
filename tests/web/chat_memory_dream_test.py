"""Tests for the nightly dream session.

The dream is the one place the bot's long-term persona is written, so the
tests here are mostly about what it *refuses* to do: never call the model for
a guild with nothing to think about, never erase a persona because one run
came back degenerate, never delete a note it did not actually read, and never
let one guild's failure take the rest of the server list down with it.

The model is always a stub — these tests assert on call counts and on rows,
never on prose.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from pydantic_ai import ModelRetry
from sqlalchemy import select

from scripts import dream_session
from smarter_dev.web import chat_memory_dream
from smarter_dev.web.chat_memory_dream import CUTOFF_SNAP_TOLERANCE
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import DreamSessionSummary
from smarter_dev.web.chat_memory_dream import build_dream_user_message
from smarter_dev.web.chat_memory_dream import dream_cutoff
from smarter_dev.web.chat_memory_dream import dream_day
from smarter_dev.web.chat_memory_dream import enforce_blob_limit
from smarter_dev.web.chat_memory_dream import get_dream_agent
from smarter_dev.web.chat_memory_dream import run_dream_session
from smarter_dev.web.chat_memory_dream import run_guild_dream
from smarter_dev.web.chat_memory_dream import should_keep_previous_blob
from smarter_dev.web.chat_memory_dream import truncate_to_last_line
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision

_GUILD = "123456789012345678"
_OTHER_GUILD = "222222222222222222"
_CHANNEL = "555000111222333444"

_CUTOFF = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
_DREAMED_AT = _CUTOFF + timedelta(minutes=20)
_YESTERDAY_MIDNIGHT = _CUTOFF - timedelta(days=1)
_YESTERDAY_MORNING = _CUTOFF - timedelta(hours=15)
_YESTERDAY_EVENING = _CUTOFF - timedelta(hours=3)

_MINUS_FIVE = timezone(timedelta(hours=-5))

_KAI_NOTE = "kai shipped the shader."
_MALLORY_NOTE = "mallory is still arguing about tabs."


# -- stubs ---------------------------------------------------------------------


@dataclass
class _StubRunResult:
    output: str


@dataclass
class _StubDreamAgent:
    """Stands in for the pydantic-ai agent: counts calls, replays scripted output.

    Scripting is keyed on a marker appearing in the rendered user message —
    a note's text — because that is the only thing that distinguishes one
    guild's dream prompt from another's.
    """

    output: str = "## Who's here\nkai (id 7) is deep in embedded rust."
    outputs_by_marker: dict[str, str] = field(default_factory=dict)
    raise_on_markers: set[str] = field(default_factory=set)
    prompts: list[str] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    async def run(self, user_prompt: str) -> _StubRunResult:
        self.prompts.append(user_prompt)
        for marker in self.raise_on_markers:
            if marker in user_prompt:
                raise RuntimeError("provider is down")
        for marker, output in self.outputs_by_marker.items():
            if marker in user_prompt:
                return _StubRunResult(output=output)
        return _StubRunResult(output=self.output)


def _session_factory_for(session):
    """A factory handing every caller the one test session, left open."""

    @contextlib.asynccontextmanager
    async def factory():
        yield session

    return factory


async def _write_note(
    session,
    *,
    content: str,
    guild_id: str = _GUILD,
    created_at: datetime = _YESTERDAY_MORNING,
    channel_name: str | None = "dev-help",
) -> ChatAgentMemoryNote:
    note = await create_memory_note(
        session,
        guild_id=guild_id,
        channel_id=_CHANNEL,
        channel_name=channel_name,
        content=content,
        created_at=created_at,
        day_start=created_at.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    await session.commit()
    return note


async def _seed_blob(session, *, content: str, guild_id: str = _GUILD):
    record = await upsert_guild_memory_blob(
        session,
        guild_id=guild_id,
        content=content,
        notes_consumed=3,
        model_name="stub-model",
        dreamed_at=_YESTERDAY_MIDNIGHT,
    )
    await session.commit()
    return record


# -- the day boundary ----------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 8, 7, 0, 0, tzinfo=UTC), datetime(2026, 8, 7, tzinfo=UTC)),
        (datetime(2026, 8, 7, 0, 20, tzinfo=UTC), datetime(2026, 8, 7, tzinfo=UTC)),
        # Well clear of the snap tolerance, so a deliberate mid-day or late
        # manual run still folds only the day that has already ended.
        (datetime(2026, 8, 7, 15, 0, tzinfo=UTC), datetime(2026, 8, 7, tzinfo=UTC)),
        (datetime(2026, 8, 7, 23, 0, tzinfo=UTC), datetime(2026, 8, 7, tzinfo=UTC)),
    ],
)
def test_dream_cutoff_is_midnight_utc_of_the_current_day(now, expected):
    assert dream_cutoff(now) == expected


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 7, 23, 59, 59, 900000, tzinfo=UTC),
        datetime(2026, 8, 7, 23, 59, 58, tzinfo=UTC),
        datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
        datetime(2026, 8, 7, 23, 55, tzinfo=UTC),
    ],
)
def test_dream_cutoff_snaps_forward_when_the_job_fires_early(now):
    """Cron jitter must not cost a whole day.

    The CronJob is scheduled for 00:00 UTC. Firing a moment early leaves ``now``
    on the previous date, and plain truncation would fold the day *before* the
    one that just ended — silently skipping 24h of notes. Anything inside the
    tolerance folds the same day the on-time run would.
    """
    assert dream_cutoff(now) == datetime(2026, 8, 8, tzinfo=UTC)
    assert dream_day(dream_cutoff(now)) == date(2026, 8, 7)


def test_dream_cutoff_agrees_across_the_midnight_boundary():
    """A hair before and a hair after midnight are the same scheduled run."""
    just_before = dream_cutoff(datetime(2026, 8, 7, 23, 59, 59, tzinfo=UTC))
    just_after = dream_cutoff(datetime(2026, 8, 8, 0, 0, 1, tzinfo=UTC))
    assert just_before == just_after == datetime(2026, 8, 8, tzinfo=UTC)


def test_dream_cutoff_does_not_snap_outside_the_tolerance():
    """Just beyond the window is a normal run, not an early fire."""
    outside = datetime(2026, 8, 7, tzinfo=UTC) + timedelta(days=1) - (
        CUTOFF_SNAP_TOLERANCE + timedelta(seconds=1)
    )
    assert dream_cutoff(outside) == datetime(2026, 8, 7, tzinfo=UTC)


def test_dream_cutoff_converts_other_zones_before_truncating():
    # 20:30 at UTC-5 is 01:30 UTC the next day — the boundary follows UTC, not
    # the wall clock of whoever happened to invoke it.
    assert dream_cutoff(datetime(2026, 8, 6, 20, 30, tzinfo=_MINUS_FIVE)) == datetime(
        2026, 8, 7, tzinfo=UTC
    )


def test_dream_cutoff_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        dream_cutoff(datetime(2026, 8, 7, 0, 20))


# -- the length ladder ---------------------------------------------------------


def test_enforce_blob_limit_passes_a_short_blob_through_stripped():
    assert enforce_blob_limit("  a short memory  ", retries_left=2) == "a short memory"


def test_enforce_blob_limit_asks_for_a_retry_while_retries_remain():
    with pytest.raises(ModelRetry):
        enforce_blob_limit("x" * (MAX_MEMORY_BLOB_CHARS + 1), retries_left=1)


def test_enforce_blob_limit_truncates_once_the_retries_are_gone():
    over_length = "\n".join(["a" * 100] * 40)
    kept = enforce_blob_limit(over_length, retries_left=0)

    assert len(kept) <= MAX_MEMORY_BLOB_CHARS
    assert kept.endswith("a" * 100)  # cut on a line boundary, not mid-sentence


def test_truncate_to_last_line_falls_back_to_a_hard_cut():
    assert truncate_to_last_line("a" * 50, 10) == "a" * 10


# -- the degenerate-output guard -----------------------------------------------


def test_short_output_against_an_established_blob_is_refused():
    assert should_keep_previous_blob("ok.", "x" * 500) is True


def test_short_output_on_a_first_night_is_accepted():
    # Nothing to protect yet, and a genuinely quiet first day is allowed to be
    # a single line.
    assert should_keep_previous_blob("kai (id 7) showed up. seems fine.", "") is False


def test_empty_output_is_always_refused():
    assert should_keep_previous_blob("   ", "") is True


# -- the user message ----------------------------------------------------------


def test_first_night_message_carries_the_no_padding_nudge():
    message = build_dream_user_message(
        previous_blob="",
        note_lines=["09:41Z #dev-help — kai got soft shadows working."],
        day=(_CUTOFF - timedelta(days=1)).date(),
    )

    assert chat_memory_dream.EMPTY_BLOB_PLACEHOLDER in message
    assert chat_memory_dream.FIRST_NIGHT_NUDGE in message


def test_a_later_night_carries_the_blob_and_no_nudge():
    message = build_dream_user_message(
        previous_blob="## Who's here\nkai (id 7)",
        note_lines=["09:41Z #dev-help — kai got soft shadows working."],
        day=(_CUTOFF - timedelta(days=1)).date(),
    )

    assert "## Who's here" in message
    assert chat_memory_dream.FIRST_NIGHT_NUDGE not in message


# -- the agent singleton -------------------------------------------------------


def test_unknown_dream_model_fails_fast(monkeypatch):
    monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)
    monkeypatch.setenv(chat_memory_dream.DREAM_MODEL_ENV_VAR, "not-a-real-model")

    with pytest.raises(RuntimeError, match="not in the catalog"):
        get_dream_agent()


def test_dream_agent_is_a_singleton(monkeypatch):
    monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)
    monkeypatch.delenv(chat_memory_dream.DREAM_MODEL_ENV_VAR, raising=False)

    first = get_dream_agent()
    assert get_dream_agent() is first
    monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)


# -- one guild's dream ---------------------------------------------------------


async def test_quiet_guild_never_calls_the_model(db_session):
    await _seed_blob(db_session, content="## Who's here\nkai (id 7)")
    agent = _StubDreamAgent()

    result = await run_guild_dream(
        db_session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_DREAMED_AT,
        agent=agent,
    )

    assert agent.call_count == 0
    assert result.outcome is DreamOutcome.SKIPPED_NO_NOTES
    stamped = await get_guild_memory_blob(db_session, _GUILD)
    assert stamped.last_dream_at.replace(tzinfo=UTC) == _DREAMED_AT
    assert stamped.content == "## Who's here\nkai (id 7)"
    assert stamped.revision == 1  # the quiet stamp is not a new revision


async def test_dream_writes_the_blob_and_consumes_only_the_notes_it_read(db_session):
    await _seed_blob(db_session, content="## Who's here\nkai (id 7)")
    consumed = await _write_note(db_session, content=_KAI_NOTE)
    written_mid_dream = await _write_note(
        db_session,
        content="mallory turned up after midnight.",
        created_at=_CUTOFF + timedelta(minutes=5),
    )
    agent = _StubDreamAgent(output="## Who's here\nkai (id 7) ships things.")

    result = await run_guild_dream(
        db_session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_DREAMED_AT,
        agent=agent,
    )

    assert agent.call_count == 1
    assert result.outcome is DreamOutcome.DREAMED
    assert result.notes_consumed == 1

    blob = await get_guild_memory_blob(db_session, _GUILD)
    assert blob.content == "## Who's here\nkai (id 7) ships things."
    assert blob.revision == 2
    assert blob.notes_consumed == 1

    surviving = list((await db_session.scalars(select(ChatAgentMemoryNote.id))).all())
    assert surviving == [written_mid_dream.id]
    assert consumed.id not in surviving

    revisions = (await db_session.scalars(select(ChatAgentMemoryRevision))).all()
    assert [revision.revision for revision in revisions] == [2]


async def test_dream_prompt_reads_the_day_oldest_first(db_session):
    await _write_note(
        db_session, content="the evening thing.", created_at=_YESTERDAY_EVENING
    )
    await _write_note(
        db_session, content="the morning thing.", created_at=_YESTERDAY_MORNING
    )
    agent = _StubDreamAgent()

    await run_guild_dream(
        db_session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_DREAMED_AT,
        agent=agent,
    )

    prompt = agent.prompts[0]
    assert prompt.index("the morning thing.") < prompt.index("the evening thing.")
    assert "#dev-help" in prompt


async def test_model_failure_leaves_the_blob_and_the_notes_untouched(db_session):
    await _seed_blob(db_session, content="## Who's here\nkai (id 7)")
    await _write_note(db_session, content=_KAI_NOTE)
    agent = _StubDreamAgent(raise_on_markers={_KAI_NOTE})

    with pytest.raises(RuntimeError):
        await run_guild_dream(
            db_session,
            guild_id=_GUILD,
            cutoff=_CUTOFF,
            dreamed_at=_DREAMED_AT,
            agent=agent,
        )
    await db_session.rollback()

    blob = await get_guild_memory_blob(db_session, _GUILD)
    assert blob.content == "## Who's here\nkai (id 7)"
    assert blob.revision == 1
    assert len((await db_session.scalars(select(ChatAgentMemoryNote.id))).all()) == 1


async def test_degenerate_output_keeps_yesterdays_blob_and_the_notes(db_session):
    established = "## Who's here\n" + "kai (id 7) is deep in embedded rust. " * 10
    await _seed_blob(db_session, content=established)
    await _write_note(db_session, content=_KAI_NOTE)
    agent = _StubDreamAgent(output="ok")

    result = await run_guild_dream(
        db_session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_DREAMED_AT,
        agent=agent,
    )

    assert result.outcome is DreamOutcome.KEPT_PREVIOUS
    blob = await get_guild_memory_blob(db_session, _GUILD)
    assert blob.content == established
    assert blob.revision == 1
    assert len((await db_session.scalars(select(ChatAgentMemoryNote.id))).all()) == 1


async def test_disabled_guild_is_never_dreamed(db_session):
    record = await _seed_blob(db_session, content="## Who's here\nkai (id 7)")
    record.memory_enabled = False
    await db_session.commit()
    await _write_note(db_session, content=_KAI_NOTE)
    agent = _StubDreamAgent()

    result = await run_guild_dream(
        db_session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_DREAMED_AT,
        agent=agent,
    )

    assert agent.call_count == 0
    assert result.outcome is DreamOutcome.SKIPPED_DISABLED
    assert len((await db_session.scalars(select(ChatAgentMemoryNote.id))).all()) == 1


# -- the whole session ---------------------------------------------------------


async def test_one_guilds_failure_never_stops_the_others(db_session):
    await _write_note(db_session, content=_KAI_NOTE, guild_id=_GUILD)
    await _write_note(db_session, content=_MALLORY_NOTE, guild_id=_OTHER_GUILD)
    agent = _StubDreamAgent(
        raise_on_markers={_KAI_NOTE},
        outputs_by_marker={_MALLORY_NOTE: "## Who's here\nmallory (id 8) argues."},
    )

    summary = await run_dream_session(
        _DREAMED_AT,
        session_factory=_session_factory_for(db_session),
        agent=agent,
    )

    assert summary.failed_guild_ids == [_GUILD]
    assert [result.guild_id for result in summary.results] == [_OTHER_GUILD]

    # The healthy guild is fully written; the failed one kept its notes, so
    # tomorrow's dream sees two days and self-heals.
    assert (await get_guild_memory_blob(db_session, _OTHER_GUILD)) is not None
    assert (await get_guild_memory_blob(db_session, _GUILD)) is None
    surviving_guilds = list(
        (await db_session.scalars(select(ChatAgentMemoryNote.guild_id))).all()
    )
    assert surviving_guilds == [_GUILD]


async def test_running_the_session_twice_in_one_night_is_a_no_op(db_session):
    await _write_note(db_session, content=_KAI_NOTE)
    agent = _StubDreamAgent()
    factory = _session_factory_for(db_session)

    await run_dream_session(_DREAMED_AT, session_factory=factory, agent=agent)
    first_pass_calls = agent.call_count
    await run_dream_session(
        _DREAMED_AT + timedelta(minutes=1), session_factory=factory, agent=agent
    )

    assert first_pass_calls == 1
    assert agent.call_count == 1  # the second pass had nothing left to read


# -- the CronJob entry point ---------------------------------------------------


async def test_script_exits_zero_when_every_guild_dreamed(monkeypatch):
    async def fake_run_dream_session(now):
        return DreamSessionSummary(results=[], failed_guild_ids=[])

    monkeypatch.setattr(dream_session, "run_dream_session", fake_run_dream_session)
    assert await dream_session.main() == 0


async def test_script_exits_one_when_any_guild_failed(monkeypatch):
    async def fake_run_dream_session(now):
        return DreamSessionSummary(results=[], failed_guild_ids=[_GUILD])

    monkeypatch.setattr(dream_session, "run_dream_session", fake_run_dream_session)
    assert await dream_session.main() == 1
