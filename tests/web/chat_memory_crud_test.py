"""Tests for the chat agent's three-layer memory crud.

Mirrors ``handler_guild_memory_test.py``: the DB round-trips run against the
real (SQLite) ``db_session`` so the dialect-switched ``ON CONFLICT`` upsert is
exercised for real rather than mocked. Covers the atomic revision bump, the
note day-window/dedupe/cap rails, revision pruning, and which guilds the dream
session is told to visit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from smarter_dev.web.crud import (
    count_notes_since,
    create_memory_note,
    delete_notes_by_id,
    get_guild_memory_blob,
    guilds_needing_dream,
    list_notes_before,
    list_notes_since,
    prune_memory_revisions,
    record_memory_revision,
    upsert_guild_memory_blob,
)
from smarter_dev.web.models import (
    ChatAgentGuildMemory,
    ChatAgentMemoryNote,
    ChatAgentMemoryRevision,
)

_GUILD = "123456789012345678"
_OTHER_GUILD = "222222222222222222"
_CHANNEL = "555000111222333444"

_MIDNIGHT = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
_MORNING = _MIDNIGHT + timedelta(hours=9)
_YESTERDAY_EVENING = _MIDNIGHT - timedelta(hours=3)


async def _save_note(
    db_session,
    *,
    content: str,
    guild_id: str = _GUILD,
    channel_name: str | None = "dev-help",
    created_at: datetime = _MORNING,
    day_start: datetime = _MIDNIGHT,
    daily_cap: int = 200,
):
    return await create_memory_note(
        db_session,
        guild_id=guild_id,
        channel_id=_CHANNEL,
        channel_name=channel_name,
        content=content,
        created_at=created_at,
        day_start=day_start,
        daily_cap=daily_cap,
    )


# -- long-term blob ------------------------------------------------------------


async def test_get_blob_is_none_for_a_guild_that_has_never_dreamed(db_session):
    assert await get_guild_memory_blob(db_session, _GUILD) is None


async def test_upsert_inserts_the_first_revision(db_session):
    record = await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="## Who's here\nkai (id 7) is deep in embedded rust.",
        notes_consumed=4,
        model_name="glm-5-2",
        dreamed_at=_MIDNIGHT,
    )
    await db_session.commit()

    assert record.revision == 1
    assert record.notes_consumed == 4
    assert record.model_name == "glm-5-2"
    assert record.memory_enabled is True
    stored = await get_guild_memory_blob(db_session, _GUILD)
    assert stored.content.startswith("## Who's here")


async def test_upsert_bumps_the_revision_in_the_statement(db_session):
    # revision+1 is computed by the ON CONFLICT SET, never read-then-written, so
    # two dreams racing on the same guild can't both write revision 2.
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="night one",
        notes_consumed=1,
        model_name="glm-5-2",
        dreamed_at=_MIDNIGHT,
    )
    await db_session.commit()
    second = await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="night two",
        notes_consumed=9,
        model_name="gemma-4-31b",
        dreamed_at=_MIDNIGHT + timedelta(days=1),
    )
    await db_session.commit()

    assert second.revision == 2
    assert second.content == "night two"
    assert second.notes_consumed == 9
    assert second.model_name == "gemma-4-31b"
    rows = (await db_session.scalars(select(ChatAgentGuildMemory))).all()
    assert len(rows) == 1  # still exactly one row per guild


async def test_upsert_never_clears_the_per_guild_kill_switch(db_session):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="a persona",
        notes_consumed=0,
        model_name=None,
        dreamed_at=_MIDNIGHT,
    )
    await db_session.commit()
    row = await get_guild_memory_blob(db_session, _GUILD)
    row.memory_enabled = False
    await db_session.commit()

    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="a new persona",
        notes_consumed=1,
        model_name=None,
        dreamed_at=_MIDNIGHT + timedelta(days=1),
    )
    await db_session.commit()

    assert (await get_guild_memory_blob(db_session, _GUILD)).memory_enabled is False


async def test_blobs_are_scoped_per_guild(db_session):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="one",
        notes_consumed=0,
        model_name=None,
        dreamed_at=_MIDNIGHT,
    )
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_OTHER_GUILD,
        content="two",
        notes_consumed=0,
        model_name=None,
        dreamed_at=_MIDNIGHT,
    )
    await db_session.commit()

    assert (await get_guild_memory_blob(db_session, _GUILD)).content == "one"
    assert (await get_guild_memory_blob(db_session, _OTHER_GUILD)).content == "two"


# -- mid-term notes ------------------------------------------------------------


async def test_create_note_stores_the_denormalised_channel(db_session):
    engagement_id = uuid4()
    record = await create_memory_note(
        db_session,
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        channel_name="dev-help",
        content="alice (id 1) got soft shadows working and was giddy about it.",
        engagement_id=engagement_id,
        created_at=_MORNING,
        day_start=_MIDNIGHT,
    )
    await db_session.commit()

    assert record is not None
    assert record.channel_name == "dev-help"
    assert record.engagement_id == engagement_id


async def test_create_note_refuses_a_same_day_duplicate(db_session):
    assert await _save_note(db_session, content="same thought") is not None
    await db_session.commit()

    assert await _save_note(db_session, content="same thought") is None
    assert await count_notes_since(db_session, _GUILD, _MIDNIGHT) == 1


async def test_the_same_thought_may_be_kept_again_on_a_later_day(db_session):
    await _save_note(
        db_session,
        content="same thought",
        created_at=_YESTERDAY_EVENING,
        day_start=_MIDNIGHT - timedelta(days=1),
    )
    await db_session.commit()

    again = await _save_note(db_session, content="same thought")
    assert again is not None


async def test_a_duplicate_in_another_guild_does_not_block_this_one(db_session):
    await _save_note(db_session, content="same thought", guild_id=_OTHER_GUILD)
    await db_session.commit()

    assert await _save_note(db_session, content="same thought") is not None


async def test_create_note_refuses_once_the_daily_cap_is_reached(db_session):
    for index in range(3):
        assert await _save_note(db_session, content=f"note {index}", daily_cap=3)
    await db_session.commit()

    assert await _save_note(db_session, content="one too many", daily_cap=3) is None
    assert await count_notes_since(db_session, _GUILD, _MIDNIGHT) == 3


async def test_count_notes_since_is_windowed_and_guild_scoped(db_session):
    await _save_note(
        db_session,
        content="yesterday",
        created_at=_YESTERDAY_EVENING,
        day_start=_MIDNIGHT - timedelta(days=1),
    )
    await _save_note(db_session, content="today")
    await _save_note(db_session, content="elsewhere", guild_id=_OTHER_GUILD)
    await db_session.commit()

    assert await count_notes_since(db_session, _GUILD, _MIDNIGHT) == 1


async def test_list_notes_since_is_newest_first_and_capped(db_session):
    for minute in range(5):
        await _save_note(
            db_session,
            content=f"note {minute}",
            created_at=_MORNING + timedelta(minutes=minute),
        )
    await db_session.commit()

    newest = await list_notes_since(db_session, _GUILD, _MIDNIGHT, limit=3)
    assert [note.content for note in newest] == ["note 4", "note 3", "note 2"]


async def test_list_notes_since_excludes_earlier_days(db_session):
    await _save_note(
        db_session,
        content="yesterday",
        created_at=_YESTERDAY_EVENING,
        day_start=_MIDNIGHT - timedelta(days=1),
    )
    await _save_note(db_session, content="today")
    await db_session.commit()

    assert [n.content for n in await list_notes_since(db_session, _GUILD, _MIDNIGHT)] == [
        "today"
    ]


async def test_list_notes_before_is_oldest_first_for_the_dream(db_session):
    day_start = _MIDNIGHT - timedelta(days=1)
    for hour in (3, 1, 2):
        await _save_note(
            db_session,
            content=f"hour {hour}",
            created_at=day_start + timedelta(hours=hour),
            day_start=day_start,
        )
    await _save_note(db_session, content="after the cutoff")
    await db_session.commit()

    consumable = await list_notes_before(db_session, _GUILD, _MIDNIGHT)
    assert [note.content for note in consumable] == ["hour 1", "hour 2", "hour 3"]


async def test_delete_notes_by_id_removes_only_the_named_rows(db_session):
    kept = await _save_note(db_session, content="keep me")
    doomed = await _save_note(db_session, content="consumed")
    await db_session.commit()

    removed = await delete_notes_by_id(db_session, [doomed.id])
    await db_session.commit()

    assert removed == 1
    remaining = (await db_session.scalars(select(ChatAgentMemoryNote))).all()
    assert [note.id for note in remaining] == [kept.id]


async def test_delete_notes_by_id_with_no_ids_is_a_no_op(db_session):
    await _save_note(db_session, content="keep me")
    await db_session.commit()

    assert await delete_notes_by_id(db_session, []) == 0
    assert await count_notes_since(db_session, _GUILD, _MIDNIGHT) == 1


# -- revision history ----------------------------------------------------------


async def test_record_memory_revision_stores_the_nights_output(db_session):
    revision = await record_memory_revision(
        db_session,
        guild_id=_GUILD,
        content="last night's blob",
        revision=3,
        notes_consumed=12,
        model_name="glm-5-2",
    )
    await db_session.commit()

    assert revision.revision == 3
    assert revision.notes_consumed == 12


async def test_prune_keeps_only_the_newest_revisions(db_session):
    for number in range(1, 8):
        await record_memory_revision(
            db_session,
            guild_id=_GUILD,
            content=f"blob {number}",
            revision=number,
            notes_consumed=0,
            model_name=None,
        )
    await record_memory_revision(
        db_session,
        guild_id=_OTHER_GUILD,
        content="somebody else's night",
        revision=1,
        notes_consumed=0,
        model_name=None,
    )
    await db_session.commit()

    removed = await prune_memory_revisions(db_session, _GUILD, keep=5)
    await db_session.commit()

    assert removed == 2
    kept = (
        await db_session.scalars(
            select(ChatAgentMemoryRevision.revision).where(
                ChatAgentMemoryRevision.guild_id == _GUILD
            )
        )
    ).all()
    assert sorted(kept) == [3, 4, 5, 6, 7]
    # Another guild's history is untouched.
    assert (
        await db_session.scalars(
            select(ChatAgentMemoryRevision.revision).where(
                ChatAgentMemoryRevision.guild_id == _OTHER_GUILD
            )
        )
    ).all() == [1]


async def test_prune_below_the_keep_count_removes_nothing(db_session):
    await record_memory_revision(
        db_session,
        guild_id=_GUILD,
        content="only night",
        revision=1,
        notes_consumed=0,
        model_name=None,
    )
    await db_session.commit()

    assert await prune_memory_revisions(db_session, _GUILD, keep=5) == 0


# -- dream scheduling ----------------------------------------------------------


async def test_guilds_needing_dream_includes_a_guild_with_pre_cutoff_notes(db_session):
    await _save_note(
        db_session,
        content="yesterday",
        created_at=_YESTERDAY_EVENING,
        day_start=_MIDNIGHT - timedelta(days=1),
    )
    await db_session.commit()

    assert await guilds_needing_dream(db_session, _MIDNIGHT) == [_GUILD]


async def test_guilds_needing_dream_ignores_notes_written_after_the_cutoff(db_session):
    await _save_note(db_session, content="written this morning")
    await db_session.commit()

    assert await guilds_needing_dream(db_session, _MIDNIGHT) == []


async def test_guilds_needing_dream_includes_a_quiet_guild_not_dreamed_yet(db_session):
    # Quiet guilds still get visited so the run can stamp last_dream_at and make
    # a CronJob retry a no-op.
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="a persona",
        notes_consumed=0,
        model_name=None,
        dreamed_at=_MIDNIGHT - timedelta(days=1),
    )
    await db_session.commit()

    assert await guilds_needing_dream(db_session, _MIDNIGHT) == [_GUILD]


async def test_guilds_needing_dream_skips_a_guild_already_dreamed_tonight(db_session):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="a persona",
        notes_consumed=0,
        model_name=None,
        dreamed_at=_MIDNIGHT + timedelta(minutes=20),
    )
    await db_session.commit()

    assert await guilds_needing_dream(db_session, _MIDNIGHT) == []


async def test_guilds_needing_dream_honours_the_per_guild_kill_switch(db_session):
    await _save_note(
        db_session,
        content="yesterday",
        created_at=_YESTERDAY_EVENING,
        day_start=_MIDNIGHT - timedelta(days=1),
    )
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="",
        notes_consumed=0,
        model_name=None,
        dreamed_at=_MIDNIGHT - timedelta(days=1),
    )
    await db_session.commit()
    row = await get_guild_memory_blob(db_session, _GUILD)
    row.memory_enabled = False
    await db_session.commit()

    assert await guilds_needing_dream(db_session, _MIDNIGHT) == []


async def test_guilds_needing_dream_returns_each_guild_once_sorted(db_session):
    day_start = _MIDNIGHT - timedelta(days=1)
    for guild_id in (_OTHER_GUILD, _GUILD):
        for index in range(2):
            await _save_note(
                db_session,
                content=f"note {index}",
                guild_id=guild_id,
                created_at=_YESTERDAY_EVENING,
                day_start=day_start,
            )
        await upsert_guild_memory_blob(
            db_session,
            guild_id=guild_id,
            content="a persona",
            notes_consumed=0,
            model_name=None,
            dreamed_at=day_start,
        )
    await db_session.commit()

    assert await guilds_needing_dream(db_session, _MIDNIGHT) == sorted(
        [_GUILD, _OTHER_GUILD]
    )
