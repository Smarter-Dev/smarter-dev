"""The CRUD Job that makes the live bump tracker use the fire's channel (#242)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from scripts import patch_bump_tracker_channel as job
from smarter_dev.web.models import AdminHandler

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live_admin_handlers"
LIVE = (FIXTURES / "live-disboard-bump-tracker.monty").read_text()
PATCHED = (FIXTURES / "after-242" / "live-disboard-bump-tracker.monty").read_text()
GUILD = "644299523686006834"


def _row(script: str = LIVE, **over) -> AdminHandler:
    values = {
        "id": uuid4(),
        "guild_id": GUILD,
        "name": "disboard-bump-tracker",
        "trigger_type": "message",
        "settings": {"include_bot_messages": True},
        "channel_ids": ["702178352395583498"],
        "description": "d",
        "script": script,
        "created_by_admin": "A1",
        "extension_handler_key": "bump-tracker",
    }
    values.update(over)
    return AdminHandler(**values)


def test_the_patch_turns_the_live_script_into_the_reviewed_fixture():
    assert job.patch_script(LIVE) == PATCHED
    assert "BUMP_CHANNEL" not in PATCHED
    assert job.is_patched(PATCHED)
    assert not job.is_patched(LIVE)


@pytest.mark.parametrize(
    "edit",
    [
        # A hand edit added a fifth delete in the fixed channel.
        lambda s: s.replace(
            "    return\n",
            '    await delete_message("X", BUMP_CHANNEL)\n    return\n',
            1,
        ),
        # The constant is gone but a use is left.
        lambda s: s.replace('BUMP_CHANNEL = "702178352395583498"\n', ""),
        # The reminder was rewritten by hand.
        lambda s: s.replace(", BUMP_CHANNEL, REMINDER_PING_ROLE", ", BUMP_CHANNEL"),
        # Another use of the constant the patch doesn't know.
        lambda s: s.replace(
            "    return best\n", "    print(BUMP_CHANNEL)\n    return best\n"
        ),
    ],
)
def test_an_unexpected_script_is_refused(edit):
    with pytest.raises(job.Refused):
        job.patch_script(edit(LIVE))


async def test_check_changes_nothing_and_update_patches_once(db_session):
    row = _row()
    db_session.add(row)
    await db_session.commit()

    planned = await job.check(db_session, GUILD)
    assert f"script sha256 {job.sha(LIVE)}" in planned[0]
    assert planned[1] == (
        f"would patch: script sha256 {job.sha(LIVE)} -> {job.sha(PATCHED)}"
    )
    await db_session.refresh(row)
    assert row.script == LIVE

    done = await job.update(db_session, GUILD, job.sha(LIVE))
    assert done == [
        f"admin_handlers id={row.id} script sha256 "
        f"{job.sha(LIVE)} -> {job.sha(PATCHED)}"
    ]
    stored = (await db_session.execute(select(AdminHandler))).scalar_one()
    assert stored.script == PATCHED
    # Nothing else on the row changes.
    assert stored.channel_ids == ["702178352395583498"]
    assert stored.settings == {"include_bot_messages": True}

    again = await job.update(db_session, GUILD, job.sha(LIVE))
    assert again == [f"admin_handlers id={row.id} already patched; nothing to update"]
    assert (await job.check(db_session, GUILD))[1] == (
        "already patched; nothing to update"
    )


async def test_update_refuses_a_script_changed_since_check(db_session):
    db_session.add(_row())
    await db_session.commit()

    with pytest.raises(job.Refused, match="run check again"):
        await job.update(db_session, GUILD, "000000000000")
    stored = (await db_session.execute(select(AdminHandler))).scalar_one()
    assert stored.script == LIVE


async def test_no_tracker_or_two_trackers_are_refused(db_session):
    with pytest.raises(job.Refused, match="0 bump trackers"):
        await job.check(db_session, GUILD)

    db_session.add(_row())
    db_session.add(_row(name="bumps-copy"))  # matched by its template key
    await db_session.commit()
    with pytest.raises(job.Refused, match="2 bump trackers"):
        await job.update(db_session, GUILD, job.sha(LIVE))


async def test_another_guilds_tracker_is_not_touched(db_session):
    other = _row(guild_id="111111111111111111")
    db_session.add(other)
    await db_session.commit()

    with pytest.raises(job.Refused):
        await job.update(db_session, GUILD, job.sha(LIVE))
    await db_session.refresh(other)
    assert other.script == LIVE
