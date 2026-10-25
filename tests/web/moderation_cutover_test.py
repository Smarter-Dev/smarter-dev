"""The cutover switch (#240): the ownership row only, refusals that change
nothing, and what the bot reads after it. There is no rollback: the legacy
actors it would restore were removed after the cutover, and since #242 their
config tables too, so the switch has nothing left to turn off."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import ownership as own
from smarter_dev.bot.moderation_coordinator.ownership import Ownership
from smarter_dev.bot.moderation_coordinator.policy import GuildPolicy
from smarter_dev.bot.moderation_coordinator.policy import PolicyCache
from smarter_dev.bot.moderation_coordinator.policy import load_policy
from smarter_dev.web import moderation_cutover as cutover
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.moderation_gateway.gateway import default_session_factory

GUILD = "644299523686006834"
BUMP_CHANNEL = "702178352395583498"
SAVED_LEGACY_STATE = {"spam_engine_enabled": True, "disboard": None}


def sessions():
    return default_session_factory()()


async def _status() -> dict:
    async with sessions() as session:
        return await cutover.status(session, GUILD)


async def _bump_tracker(script: str = "pass\n") -> None:
    async with sessions() as session:
        session.add(
            AdminHandler(
                guild_id=GUILD,
                name="disboard-bump-tracker",
                trigger_type="message",
                settings={},
                channel_ids=[BUMP_CHANNEL],
                description="d",
                script=script,
                created_by_admin="extension",
                enabled=True,
            )
        )
        await session.commit()


async def test_status_of_a_guild_never_switched():
    status = await _status()
    assert status == {
        "guild_id": GUILD,
        "owner": own.LEGACY,
        "generation": 0,
        "legacy_state_saved": False,
        "changed_by": None,
        "changed_at": None,
        "policy_revision": None,
    }


async def test_the_switch_writes_the_ownership_row_and_nothing_else():
    await _bump_tracker()
    async with sessions() as session:
        after = await cutover.switch(session, GUILD, by="zech")

    assert after["owner"] == own.COORDINATOR and after["generation"] == 1
    assert after["changed_by"] == "zech"
    assert after["legacy_state_saved"] is False
    assert after["policy_revision"] is None
    async with sessions() as session:
        row = await session.get(ModerationOwnership, GUILD)
        assert row.rules is None and row.legacy_state is None
        # The Disboard handler is not touched: the bump channel is the
        # policy's rule now.
        handler = (await session.execute(select(AdminHandler))).scalar_one()
        assert handler.enabled is True and handler.script == "pass\n"


async def test_a_switch_back_to_the_coordinator_keeps_the_old_record():
    """A row a first cutover wrote keeps its rules and legacy_state as data."""
    async with sessions() as session:
        session.add(
            ModerationOwnership(
                guild_id=GUILD,
                owner=own.LEGACY,
                generation=3,
                rules=["spam_engine"],
                legacy_state=SAVED_LEGACY_STATE,
                changed_by="old",
            )
        )
        await session.commit()
    async with sessions() as session:
        after = await cutover.switch(session, GUILD, by="zech")
    assert after["owner"] == own.COORDINATOR and after["generation"] == 4
    assert after["legacy_state_saved"] is True
    async with sessions() as session:
        row = await session.get(ModerationOwnership, GUILD)
        assert row.rules == ["spam_engine"]
        assert row.legacy_state == SAVED_LEGACY_STATE


async def test_a_second_switch_is_refused_and_changes_nothing():
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")

    async with sessions() as session:
        with pytest.raises(cutover.CutoverRefused, match="already owned"):
            await cutover.switch(session, GUILD, by="again")
        await session.rollback()
    async with sessions() as session:
        row = await session.get(ModerationOwnership, GUILD)
        assert row.generation == 1
        assert row.changed_by == "zech"


async def test_the_command_line_exits_two_on_a_refusal(monkeypatch):
    """The Job fails (exit 2) and nothing changes."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "moderation_cutover.py"
    spec = importlib.util.spec_from_file_location("moderation_cutover_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "get_db_session_context", default_session_factory())
    monkeypatch.setenv("MODERATION_SHADOW_GUILD_IDS", "1")
    # Not a guild the bot listens to: refused.
    assert await script.main(["switch", "--guild", GUILD, "--by", "zech"]) == 2
    assert (await _status())["generation"] == 0
    monkeypatch.setenv("MODERATION_SHADOW_GUILD_IDS", f"1, {GUILD}")
    assert await script.main(["status", "--guild", GUILD]) == 0
    # The rollback command is gone with the legacy actors it restored.
    with pytest.raises(SystemExit):
        await script.main(["rollback", "--guild", GUILD])
    assert await script.main(["switch", "--guild", GUILD, "--by", "zech"]) == 0
    assert await script.main(["switch", "--guild", GUILD]) == 2
    assert (await _status())["generation"] == 1


# -- what the bot reads -------------------------------------------------------


async def test_a_switched_guild_without_a_policy_runs_nothing():
    """The coordinator owns it, but until the import writes a policy no rule
    runs, nothing is enforced and nothing is reported."""
    await _bump_tracker()
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")

    loaded = await load_policy(GUILD)
    assert loaded.acting and loaded.generation == 1
    assert loaded.revision is None
    assert loaded.spam is None and loaded.ring is None
    assert loaded.attachments is None
    assert not loaded.scam_gate and not loaded.edit_ping
    assert not loaded.mass_mention
    assert loaded.bump_channel_ids == frozenset()
    assert loaded.enforced == frozenset()
    assert not any(loaded.enforces(rule) for rule in own.RULES)
    assert loaded.staff_exempt_role_ids == ()
    assert loaded.report_channel_id is None


async def test_the_policy_cache_follows_a_switch_on_the_next_message():
    """No TTL-long gap: the moment the guild is switched, the coordinator
    acts."""
    owner = {"value": Ownership(GUILD)}
    loads = []

    async def loader(guild_id):
        current = owner["value"]
        loads.append(current.generation)
        return GuildPolicy(
            guild_id=guild_id, owner=current.owner, generation=current.generation
        )

    async def ownership(guild_id):
        return owner["value"]

    cache = PolicyCache(loader=loader, ownership=ownership)
    now = datetime(2026, 10, 23, tzinfo=UTC).timestamp()
    assert not (await cache.get(GUILD, now)).acting
    assert not (await cache.get(GUILD, now + 1)).acting
    assert loads == [0]
    owner["value"] = Ownership(GUILD, own.COORDINATOR, 1)
    assert (await cache.get(GUILD, now + 2)).acting
    assert loads == [0, 1]
