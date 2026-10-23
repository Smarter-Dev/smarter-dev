"""The cutover switch (#240): one transaction each way, refusals that change
nothing, and the policy the bot reads from it."""

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
from smarter_dev.extensions.registry import get_registry
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web import moderation_cutover as cutover
from smarter_dev.web.handler_moderation_policy import SCAM_BANNER_HANDLER
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import AttachmentFilterConfig
from smarter_dev.web.models import ExtensionInstall
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.moderation_gateway.gateway import default_session_factory

GUILD = "644299523686006834"
BUMP_CHANNEL = "702178352395583498"
DISBOARD_CONFIG = {
    "bump_channel_id": BUMP_CHANNEL,
    "bump_king_role_id": "715584554861330465",
    "bump_king_announcement_channel_id": "1266565514793062400",
    "reminder_ping_role_id": "702177774315634788",
}


def sessions():
    return default_session_factory()()


def _v3_disboard_script() -> str:
    """The live install's script: the catalog's bump tracker, rendered with
    smarter.dev's config (members' chatter deleted by the handler)."""
    loaded = get_registry().get(cutover.DISBOARD_SLUG)
    [item] = render_bundle(loaded.manifest, DISBOARD_CONFIG, loaded.scripts)
    assert cutover.DISBOARD_RESTRICTION in item.script
    return item.script


async def _production_like() -> None:
    """smarter.dev as it is on Oct 23: every legacy actor on."""
    async with sessions() as session:
        session.add(
            ModerationFilterConfig(
                guild_id=GUILD, spam_engine_enabled=True, spam_ring_enabled=True
            )
        )
        session.add(AttachmentFilterConfig(guild_id=GUILD, is_active=True))
        edit_install = ExtensionInstall(
            guild_id=GUILD,
            extension_slug=cutover.EDIT_PING_SLUG,
            installed_version=1,
            config={"staff_role_id": "644390354157568014"},
            enabled=True,
            installed_by="admin",
        )
        disboard_install = ExtensionInstall(
            guild_id=GUILD,
            extension_slug=cutover.DISBOARD_SLUG,
            installed_version=3,
            config=DISBOARD_CONFIG,
            enabled=True,
            installed_by="admin",
        )
        session.add_all([edit_install, disboard_install])
        await session.flush()
        for name, install, channels, script in (
            (SCAM_BANNER_HANDLER, None, [], "pass\n"),
            (cutover.EDIT_PING_HANDLER, edit_install, [], "pass\n"),
            (
                cutover.BUMP_HANDLER,
                disboard_install,
                [BUMP_CHANNEL],
                _v3_disboard_script(),
            ),
        ):
            session.add(
                AdminHandler(
                    guild_id=GUILD,
                    name=name,
                    trigger_type="message",
                    settings={},
                    channel_ids=channels,
                    description="d",
                    script=script,
                    created_by_admin="extension" if install else "A",
                    enabled=True,
                    extension_install_id=install.id if install else None,
                    extension_handler_key="bump_tracker"
                    if name == cutover.BUMP_HANDLER
                    else None,
                )
            )
        await session.commit()


async def _status() -> dict:
    async with sessions() as session:
        return await cutover.status(session, GUILD)


async def test_status_reads_everything_the_switch_touches():
    await _production_like()
    status = await _status()
    assert status["owner"] == own.LEGACY and status["generation"] == 0
    assert status["rollback_saved"] is False
    assert status["spam_engine_enabled"] and status["spam_ring_enabled"]
    assert status["attachment_filter_active"] is True
    assert set(status["handlers"]) == {
        SCAM_BANNER_HANDLER,
        cutover.EDIT_PING_HANDLER,
        cutover.BUMP_HANDLER,
    }
    assert all(h["enabled"] for h in status["handlers"].values())
    assert status["extension_installs"][cutover.DISBOARD_SLUG]["installed_version"] == 3


async def test_the_switch_hands_over_in_one_go_and_rollback_restores_it():
    await _production_like()
    before = await _status()
    async with sessions() as session:
        after = await cutover.switch(session, GUILD, by="zech")

    assert after["owner"] == own.COORDINATOR and after["generation"] == 1
    assert set(after["rules"]) == set(own.RULES)
    assert after["rollback_saved"] is True
    assert after["spam_engine_enabled"] is False
    assert after["spam_ring_enabled"] is False
    assert after["attachment_filter_active"] is False
    assert after["handlers"][SCAM_BANNER_HANDLER]["enabled"] is False
    assert after["handlers"][cutover.EDIT_PING_HANDLER]["enabled"] is False
    assert after["extension_installs"][cutover.EDIT_PING_SLUG]["enabled"] is False
    # Disboard keeps running, at the version that leaves chatter to moderation.
    bump = after["handlers"][cutover.BUMP_HANDLER]
    assert bump["enabled"] is True
    assert (
        bump["script_sha256"]
        != before["handlers"][cutover.BUMP_HANDLER]["script_sha256"]
    )
    # Only the live row: the catalog (and so every legacy guild) is unchanged.
    assert after["extension_installs"][cutover.DISBOARD_SLUG]["installed_version"] == 3
    async with sessions() as session:
        script = (
            await session.execute(
                select(AdminHandler.script).where(
                    AdminHandler.name == cutover.BUMP_HANDLER
                )
            )
        ).scalar_one()
    assert cutover.DISBOARD_BOTS_ONLY in script
    assert cutover.DISBOARD_RESTRICTION not in script
    # Rows are disabled, never deleted.
    async with sessions() as session:
        assert len((await session.execute(select(AdminHandler))).all()) == 3

    async with sessions() as session:
        back = await cutover.rollback(session, GUILD, by="zech")
    assert back["owner"] == own.LEGACY and back["generation"] == 2
    assert back["rollback_saved"] is False
    assert back["notes"] == []
    for key in (
        "spam_engine_enabled",
        "spam_ring_enabled",
        "attachment_filter_active",
        "handlers",
        "extension_installs",
    ):
        assert back[key] == before[key], key


async def test_a_second_switch_is_refused_and_keeps_the_saved_rollback():
    await _production_like()
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")
    async with sessions() as session:
        saved = (await session.get(ModerationOwnership, GUILD)).legacy_state

    async with sessions() as session:
        with pytest.raises(cutover.CutoverRefused, match="already owned"):
            await cutover.switch(session, GUILD, by="again")
        await session.rollback()
    async with sessions() as session:
        row = await session.get(ModerationOwnership, GUILD)
        assert row.generation == 1
        assert row.legacy_state == saved
        assert row.changed_by == "zech"


async def test_rollback_with_nothing_saved_is_refused():
    await _production_like()
    async with sessions() as session:
        with pytest.raises(cutover.CutoverRefused, match="nothing saved"):
            await cutover.rollback(session, GUILD, by="zech")
        await session.rollback()
    assert (await _status())["owner"] == own.LEGACY
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")
    async with sessions() as session:
        await cutover.rollback(session, GUILD, by="zech")
    async with sessions() as session:
        with pytest.raises(cutover.CutoverRefused, match="nothing saved"):
            await cutover.rollback(session, GUILD, by="zech")


async def test_rollback_keeps_a_disboard_script_edited_since_the_switch():
    await _production_like()
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")
    async with sessions() as session:
        row = (
            await session.execute(
                select(AdminHandler).where(AdminHandler.name == cutover.BUMP_HANDLER)
            )
        ).scalar_one()
        row.script += "\n# an admin's edit\n"
        await session.commit()
    async with sessions() as session:
        back = await cutover.rollback(session, GUILD, by="zech")
    assert back["notes"] and "Disboard" in back["notes"][0]
    async with sessions() as session:
        script = (
            await session.execute(
                select(AdminHandler.script).where(
                    AdminHandler.name == cutover.BUMP_HANDLER
                )
            )
        ).scalar_one()
    assert script.endswith("# an admin's edit\n")


async def test_a_guild_without_a_filter_config_is_refused():
    async with sessions() as session:
        with pytest.raises(cutover.CutoverRefused, match="moderation_filter_configs"):
            await cutover.switch(session, "1", by="zech")


async def test_the_command_line_exits_two_on_a_refusal(monkeypatch):
    """The Job fails (exit 2) and nothing changes."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "moderation_cutover.py"
    spec = importlib.util.spec_from_file_location("moderation_cutover_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "get_db_session_context", default_session_factory())
    await _production_like()
    monkeypatch.setenv("MODERATION_SHADOW_GUILD_IDS", "1")
    # Not a guild the bot listens to: refused.
    assert await script.main(["switch", "--guild", GUILD, "--by", "zech"]) == 2
    monkeypatch.setenv("MODERATION_SHADOW_GUILD_IDS", f"1, {GUILD}")
    assert await script.main(["status", "--guild", GUILD]) == 0
    assert await script.main(["rollback", "--guild", GUILD]) == 2
    assert await script.main(["switch", "--guild", GUILD, "--by", "zech"]) == 0
    assert await script.main(["switch", "--guild", GUILD]) == 2
    assert (await _status())["generation"] == 1


# -- what the bot reads -------------------------------------------------------


async def test_a_switched_guild_runs_the_saved_rules_with_the_legacy_settings():
    await _production_like()
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")

    loaded = await load_policy(GUILD)
    assert loaded.acting and loaded.generation == 1
    # Every legacy switch is off now, and every rule still runs, with the
    # actors' own settings.
    assert loaded.spam is not None and loaded.ring is not None
    assert loaded.attachments is not None
    assert loaded.scam_gate and loaded.edit_ping
    assert loaded.bump_channel_ids == frozenset({BUMP_CHANNEL})

    async with sessions() as session:
        await cutover.rollback(session, GUILD, by="zech")
    back = await load_policy(GUILD)
    assert not back.acting and back.generation == 2
    assert back.spam is not None and back.scam_gate  # shadow, as before


async def test_a_rule_left_out_of_the_switch_stays_off():
    await _production_like()
    async with sessions() as session:
        row = (
            await session.execute(
                select(AdminHandler).where(AdminHandler.name == SCAM_BANNER_HANDLER)
            )
        ).scalar_one()
        row.enabled = False  # scam-banner was already off
        await session.commit()
    async with sessions() as session:
        result = await cutover.switch(session, GUILD, by="zech")
    assert own.SCAM_GATE not in result["rules"]
    assert not (await load_policy(GUILD)).scam_gate


async def test_the_policy_cache_follows_a_switch_on_the_next_message():
    """No TTL-long gap: the moment the legacy actors are off, the coordinator
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
    owner["value"] = Ownership(GUILD, own.COORDINATOR, 1, frozenset(own.RULES))
    assert (await cache.get(GUILD, now + 2)).acting
    assert loads == [0, 1]


async def test_a_disboard_script_that_is_not_the_catalogs_refuses_the_switch():
    await _production_like()
    async with sessions() as session:
        row = (
            await session.execute(
                select(AdminHandler).where(AdminHandler.name == cutover.BUMP_HANDLER)
            )
        ).scalar_one()
        row.script = "pass\n"
        await session.commit()
    async with sessions() as session:
        with pytest.raises(cutover.CutoverRefused, match="Disboard"):
            await cutover.switch(session, GUILD, by="zech")
        await session.rollback()
    status = await _status()
    assert status["owner"] == own.LEGACY
    assert status["spam_engine_enabled"] is True


async def test_the_patched_disboard_tracker_leaves_members_to_moderation():
    """Run the switched script: a member's chatter stays, another bot's goes."""
    from smarter_dev.web.handler_budget import admin_budget
    from smarter_dev.web.handler_runtime import run_handler_script
    from tests.test_disboard_bumping_extension import _Actor
    from tests.test_disboard_bumping_extension import _Emitter
    from tests.test_disboard_bumping_extension import _Limiter
    from tests.test_disboard_bumping_extension import _TimerRecorder

    async def _run_tracker_script(script, context):
        actor = _Actor()
        result = await run_handler_script(
            script,
            context,
            channel_id=BUMP_CHANNEL,
            guild_id="G1",
            emitter=_Emitter(),
            limiter=_Limiter(),
            budget=admin_budget("message"),
            actor=actor,
            channel_ids=[BUMP_CHANNEL],
            allowed_role_ids=["715584554861330465"],
            guild_memory={},
            timer_scheduler=_TimerRecorder(),
            handler_id="H-tracker",
        )
        assert result.outcome == "ok", result.error
        return actor.calls

    await _production_like()
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")
        script = (
            await session.execute(
                select(AdminHandler.script).where(
                    AdminHandler.name == cutover.BUMP_HANDLER
                )
            )
        ).scalar_one()
    member = await _run_tracker_script(
        script,
        {
            "trigger_type": "message",
            "author_id": "555",
            "author_is_bot": False,
            "message_id": "M1",
        },
    )
    bot = await _run_tracker_script(
        script,
        {
            "trigger_type": "message",
            "author_id": "556",
            "author_is_bot": True,
            "message_id": "B1",
        },
    )
    assert member == []
    assert bot == [("delete", BUMP_CHANNEL, "B1")]
