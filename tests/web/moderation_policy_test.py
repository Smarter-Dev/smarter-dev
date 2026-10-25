"""#241: the policy document, its store, the import and the revision pickup.

The import runs on smarter.dev as it is after the cutover (#240): the
cutover's own ``switch`` turns the legacy actors off first, so the import is
shown reading the coordinator's rules, not the switches the cutover zeroed.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import select

from smarter_dev.bot import triage_config
from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.moderation_coordinator import classifier
from smarter_dev.bot.moderation_coordinator import coordinator
from smarter_dev.bot.moderation_coordinator import decide
from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.ownership import load_ownership
from smarter_dev.bot.moderation_coordinator.policy import PolicyCache
from smarter_dev.bot.moderation_coordinator.policy import load_policy
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web import moderation_cutover as cutover
from smarter_dev.web import moderation_policy_import as policy_import
from smarter_dev.web import moderation_policy_store as store
from smarter_dev.web.models import ModerationConfig
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationPolicyLoad
from smarter_dev.web.models import ModerationPolicyRevision
from smarter_dev.web.moderation_gateway.config import load_incident_report_channel
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from tests.web.moderation_cutover_test import BUMP_CHANNEL
from tests.web.moderation_cutover_test import GUILD
from tests.web.moderation_cutover_test import _production_like

REPORTS = "728249959098482829"
MODS = "644390354157568014"
ADMIN = "644301991832453120"


def sessions():
    return default_session_factory()()


async def _switched(*, triage_tools=("timeout", "purge", "delete")) -> None:
    """smarter.dev after the cutover: switched, report channels and triage set."""
    await _production_like()
    async with sessions() as session:
        config = await session.get(ModerationFilterConfig, GUILD)
        config.staff_exempt_role_ids = [MODS, ADMIN]
        config.mod_alert_channel_id = REPORTS
        config.mod_ping_role_id = MODS
        session.add(
            ModerationConfig(
                guild_id=GUILD,
                is_active=True,
                monitored_role_ids=[MODS, ADMIN],
                instructions="Freeze, don't judge.",
                enabled_tools=list(triage_tools),
                response_channel_id=REPORTS,
                context_message_limit=25,
            )
        )
        await session.commit()
    async with sessions() as session:
        await cutover.switch(session, GUILD, by="zech")


async def _plan() -> policy_import.ImportPlan:
    async with sessions() as session:
        return await policy_import.plan(session, GUILD)


# -- the document -----------------------------------------------------------------


def test_the_defaults_are_the_values_the_code_ran_with():
    """The policy replaces these constants; a guild without one runs on them,
    so a default that drifts from its constant changes nothing silently."""
    doc = mp.default_document()
    assert doc.risk.windows.model_dump() == rules.RISK_SECONDS
    assert doc.risk.joined_within_seconds == rules.DAY
    assert doc.sanctions.scam_ban_account_age_days == rules.BAN_ACCOUNT_AGE.days
    assert doc.sanctions.scam_timeout_seconds == rules.REPEAT_TIMEOUT_SECONDS
    assert doc.sanctions.repeat_window_seconds == rules.RECENT_ACTION_SECONDS
    assert doc.provisional.duration_seconds == decide.PROVISIONAL_SECONDS
    assert set(doc.provisional.signals) == set(rules.SIGNAL_OF.values())
    assert doc.rules.scam_gate.image_sample == coordinator.SAMPLED_IMAGES
    assert doc.classifier.deadline_seconds == classifier.DEADLINE_SECONDS
    assert doc.classifier.per_author == classifier.PER_AUTHOR
    assert doc.classifier.author_window_seconds == classifier.AUTHOR_WINDOW_SECONDS
    assert doc.classifier.per_guild_minute == classifier.PER_GUILD_MINUTE
    assert doc.classifier.per_guild_day == classifier.DEFAULT_ACTING_DAILY_REQUESTS
    assert (
        policy_import.DEFAULT_ACTING_DAILY_REQUESTS
        == classifier.DEFAULT_ACTING_DAILY_REQUESTS
    )
    assert doc.sanctions.max_timeout_seconds == mp.DISCORD_MAX_TIMEOUT_SECONDS
    assert set(mp.TRIAGE_TOOLS) == set(triage_config.DESTRUCTIVE_TOOLS)
    model = ModerationFilterConfig.get_defaults(GUILD)
    spam = doc.rules.spam_engine
    for name in policy_import._SPAM_FIELDS:
        assert getattr(spam, name) == getattr(model, name), name
    ring = doc.rules.spam_ring
    assert ring.new_member_days == model.spam_ring_new_member_days
    assert ring.timeout_seconds == model.spam_ring_timeout_seconds
    assert ring.lookback_minutes == model.spam_ring_lookback_minutes
    assert ring.followup_minutes == model.spam_ring_followup_minutes
    # The triage caps are code, not settings.
    assert mod_tools.MAX_PURGE_COUNT == 50


def test_unknown_keys_and_bad_values_are_refused_by_name():
    raw = mp.default_document().as_json()
    with pytest.raises(mp.PolicyError) as error:
        mp.validate({**raw, "rulez": {}})
    assert error.value.errors == ["rulez: Extra inputs are not permitted"]
    raw["rules"]["scam_gate"]["mode"] = "on"
    raw["destinations"]["report_channel_id"] = "#mod-log"
    raw["sanctions"]["allowed"] = ["kick"]
    with pytest.raises(mp.PolicyError) as error:
        mp.validate(raw)
    fields = {line.split(":")[0] for line in error.value.errors}
    assert fields == {
        "rules.scam_gate.mode",
        "destinations.report_channel_id",
        "sanctions.allowed.0",
    }


def test_an_empty_list_stays_empty():
    raw = mp.default_document().as_json()
    raw["sanctions"]["allowed"] = []
    raw["provisional"]["signals"] = []
    raw["triage"]["enabled_tools"] = []
    doc = mp.validate(raw)
    assert doc.sanctions.allowed == []
    assert doc.provisional.signals == []
    assert doc.triage.enabled_tools == []
    assert mp.validate(doc.as_json()) == doc


def test_a_scam_timeout_longer_than_the_longest_allowed_is_refused():
    raw = mp.default_document().as_json()
    raw["sanctions"]["max_timeout_seconds"] = 3600
    with pytest.raises(mp.PolicyError, match="scam_timeout_seconds"):
        mp.validate(raw)


def test_guild_wide_controls_override_each_rule():
    raw = mp.default_document().as_json()
    raw["rules"]["spam_engine"]["mode"] = mp.ENFORCE
    assert mp.validate(raw).mode(mp.SPAM_ENGINE) == mp.ENFORCE
    raw["controls"]["enforce"] = False
    assert mp.validate(raw).mode(mp.SPAM_ENGINE) == mp.EVALUATE
    raw["controls"]["evaluate"] = False
    assert mp.validate(raw).mode(mp.SPAM_ENGINE) == mp.OFF


# -- the import -------------------------------------------------------------------


async def test_the_import_reads_the_coordinators_rules_not_the_zeroed_switches():
    await _switched()
    plan = await _plan()
    doc = plan.document

    # Every rule the cutover handed over (and mass mention) enforces, though
    # the old tables' switches now read off.
    assert {rule: doc.rule(rule).mode for rule in mp.RULES} == dict.fromkeys(
        mp.RULES, mp.ENFORCE
    )
    async with sessions() as session:
        config = await session.get(ModerationFilterConfig, GUILD)
        assert config.spam_engine_enabled is False
    assert doc.protected_role_ids == [MODS, ADMIN]
    assert doc.destinations.report_channel_id == REPORTS
    assert doc.destinations.mod_ping_role_id == MODS
    assert doc.rules.bump_channel.channels == [BUMP_CHANNEL]
    assert doc.rules.scam_gate.channels == [] and doc.rules.edit_ping.channels == []
    assert doc.rules.spam_engine.scam_link_domains == list(
        ModerationFilterConfig.get_defaults(GUILD).scam_link_domains
    )
    assert doc.triage.mode == mp.ENFORCE
    assert doc.triage.enabled_tools == ["timeout", "purge", "delete"]
    assert doc.triage.monitored_role_ids == [MODS, ADMIN]
    assert doc.classifier.per_guild_day == 1500
    assert plan.sources["ownership"]["generation"] == 1
    assert plan.notes == []


async def test_an_empty_triage_allowlist_is_imported_empty():
    await _switched(triage_tools=())
    assert (await _plan()).document.triage.enabled_tools == []


async def test_the_acting_daily_ceiling_comes_from_the_bots_setting(monkeypatch):
    monkeypatch.setenv(policy_import.ACTING_DAILY_REQUESTS_ENV_VAR, "900")
    await _switched()
    plan = await _plan()
    assert plan.document.classifier.per_guild_day == 900
    assert plan.sources["classifier.per_guild_day_from"] == (
        policy_import.ACTING_DAILY_REQUESTS_ENV_VAR
    )


async def test_a_guild_the_coordinator_does_not_own_is_refused():
    await _production_like()
    with pytest.raises(policy_import.ImportRefused, match="not moderated"):
        await _plan()


async def test_the_import_writes_revision_one_once():
    await _switched()
    async with sessions() as session:
        await policy_import.run(session, GUILD, by="zech")
    async with sessions() as session:
        stored = await store.read_policy(session, GUILD)
        [revision] = await store.revisions(session, GUILD)
    assert stored.revision == 1 and stored.updated_by == "zech"
    assert revision.source == store.SOURCE_IMPORT and revision.revision == 1
    with pytest.raises(policy_import.ImportRefused, match="already has"):
        await _plan()


async def test_the_dry_run_writes_nothing():
    await _switched()
    await _plan()
    async with sessions() as session:
        assert await store.read_policy(session, GUILD) is None


# -- saving -----------------------------------------------------------------------


async def _imported() -> mp.PolicyDocument:
    await _switched()
    async with sessions() as session:
        return (await policy_import.run(session, GUILD, by="import")).document


async def test_a_save_on_a_stale_revision_is_refused_and_writes_nothing():
    doc = await _imported()
    edited = doc.as_json()
    edited["rules"]["spam_ring"]["mode"] = mp.EVALUATE
    async with sessions() as session:
        assert (
            await store.save_policy(
                session, GUILD, edited, base_revision=1, by="a", source="admin"
            )
            == 2
        )
    async with sessions() as session:
        with pytest.raises(store.StaleRevision):
            await store.save_policy(
                session, GUILD, doc.as_json(), base_revision=1, by="b", source="admin"
            )
    async with sessions() as session:
        stored = await store.read_policy(session, GUILD)
        latest = (await store.revisions(session, GUILD))[0]
    assert stored.revision == 2
    assert stored.document.rules.spam_ring.mode == mp.EVALUATE
    assert latest.changed_fields == ["rules.spam_ring.mode"]


async def test_an_invalid_document_is_not_saved():
    await _imported()
    async with sessions() as session:
        with pytest.raises(mp.PolicyError):
            await store.save_policy(
                session,
                GUILD,
                {"schema": 1, "x": 1},
                base_revision=1,
                by="a",
                source="api",
            )
    async with sessions() as session:
        assert (await store.read_policy(session, GUILD)).revision == 1


# -- the bot picks it up ------------------------------------------------------------


async def test_two_processes_pick_up_a_saved_revision_on_their_next_message():
    doc = await _imported()
    first = PolicyCache(ownership=load_ownership)
    second = PolicyCache(ownership=load_ownership)
    for cache in (first, second):
        policy = await cache.get(GUILD)
        assert policy.revision == 1 and policy.ring is not None

    edited = doc.as_json()
    edited["rules"]["spam_ring"]["mode"] = mp.OFF
    async with sessions() as session:
        await store.save_policy(
            session, GUILD, edited, base_revision=1, by="zech", source="admin"
        )
    for cache in (first, second):
        # Well inside the TTL: the revision read alone reloads it.
        policy = await cache.get(GUILD)
        assert policy.revision == 2 and policy.ring is None
        assert cache.loaded()[GUILD][0] == 2


async def test_a_policy_in_evaluate_is_judged_but_not_enforced():
    doc = await _imported()
    edited = doc.as_json()
    edited["rules"]["scam_gate"]["mode"] = mp.EVALUATE
    async with sessions() as session:
        await store.save_policy(
            session, GUILD, edited, base_revision=1, by="zech", source="admin"
        )
    policy = await load_policy(GUILD)
    assert policy.scam_gate is True
    assert not policy.enforces(mp.SCAM_GATE) and policy.enforces(mp.SPAM_ENGINE)
    assert not policy.provisional_allowed()


async def test_reporting_off_reports_nowhere():
    doc = await _imported()
    edited = doc.as_json()
    edited["controls"]["report"] = False
    async with sessions() as session:
        await store.save_policy(
            session, GUILD, edited, base_revision=1, by="zech", source="admin"
        )
    assert (await load_policy(GUILD)).report_channel_id is None
    async with sessions() as session:
        assert await load_incident_report_channel(session, GUILD) is None


async def test_without_a_policy_the_old_tables_are_read_as_before():
    await _switched()
    policy = await load_policy(GUILD)
    assert policy.revision is None and policy.enforced is None
    assert policy.spam is not None and policy.report_channel_id == REPORTS


def test_triage_follows_the_policys_mode():
    raw = mp.default_document().as_json()
    raw["triage"].update(
        mode=mp.ENFORCE,
        monitored_role_ids=[MODS],
        enabled_tools=["timeout"],
        response_channel_id=REPORTS,
    )
    config = triage_config.triage_from_policy(GUILD, mp.validate(raw))
    assert config.enabled_tools == ("timeout",)
    assert config.response_channel_id == REPORTS
    raw["triage"]["mode"] = mp.EVALUATE
    assert triage_config.triage_from_policy(GUILD, mp.validate(raw)).enabled_tools == ()
    raw["triage"]["mode"] = mp.ENFORCE
    raw["controls"]["enforce"] = False
    assert triage_config.triage_from_policy(GUILD, mp.validate(raw)).enabled_tools == ()
    raw["triage"]["mode"] = mp.OFF
    assert triage_config.triage_from_policy(GUILD, mp.validate(raw)) is None


# -- which process runs which revision ------------------------------------------------


async def test_loads_are_upserted_per_process_and_stale_ones_pruned():
    now = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    async with sessions() as session:
        await store.record_loads(session, "bot/a", {GUILD: (1, now)}, now)
        await store.record_loads(session, "bot/gone", {GUILD: (1, now)}, now)
    later = now + timedelta(hours=25)
    async with sessions() as session:
        await store.record_loads(session, "bot/a", {GUILD: (2, later)}, later)
    async with sessions() as session:
        rows = (await session.execute(select(ModerationPolicyLoad))).scalars().all()
    assert [(r.process, r.revision) for r in rows] == [("bot/a", 2)]


async def test_the_heartbeat_reports_what_the_cache_loaded():
    from smarter_dev.bot.moderation_coordinator.shadow import report_loads

    await _imported()
    cache = PolicyCache(ownership=load_ownership)
    await cache.get(GUILD)
    await report_loads(cache, process="bot/test")
    async with sessions() as session:
        [row] = await store.loads(session, GUILD)
        revisions = (await session.execute(select(ModerationPolicyRevision))).scalars()
    assert (row.process, row.revision) == ("bot/test", 1)
    assert [r.revision for r in revisions] == [1]


PARITY_FIELDS = (
    "spam",
    "attachments",
    "ring",
    "scam_gate",
    "edit_ping",
    "scam_gate_channel_ids",
    "edit_ping_channel_ids",
    "bump_channel_ids",
    "mass_mention",
    "staff_exempt_role_ids",
    "owner",
    "generation",
    "report_channel_id",
)


async def test_after_the_import_the_coordinator_runs_exactly_as_before():
    """The imported revision gives the coordinator the very settings it read
    from the old tables, field by field."""
    await _switched()
    before = await load_policy(GUILD)
    async with sessions() as session:
        await policy_import.run(session, GUILD, by="import")
    after = await load_policy(GUILD)
    assert after.revision == 1
    # The legacy engine's own switch and channel, which the coordinator never
    # reads: the cutover zeroed the one, the other is the report channel.
    before = replace(
        before,
        spam=replace(before.spam, enabled=True, report_channel_id=REPORTS),
        ring=replace(before.ring, enabled=True),
    )
    for name in PARITY_FIELDS:
        assert getattr(after, name) == getattr(before, name), name
    assert after.document.as_json() != mp.default_document().as_json()


# -- review of 09f3f973 (#364) ----------------------------------------------------


async def test_triage_keeps_its_own_reply_channel_and_its_fallback():
    """Fix 1: triage replied in ``moderation_configs.response_channel_id``,
    else in the ping's channel; the import keeps both, and the report
    channel does not stand in for it."""
    await _switched()
    async with sessions() as session:
        triage = await session.get(ModerationConfig, GUILD)
        triage.response_channel_id = "702178352395580009"
        await session.commit()
    doc = (await _plan()).document
    assert doc.triage.response_channel_id == "702178352395580009"
    assert doc.destinations.report_channel_id == REPORTS
    config = triage_config.triage_from_policy(GUILD, doc)
    assert config.response_channel_id == "702178352395580009"

    raw = doc.as_json()
    raw["triage"]["response_channel_id"] = None
    config = triage_config.triage_from_policy(GUILD, mp.validate(raw))
    assert config.response_channel_id is None  # the ping's own channel


async def test_a_missing_config_row_imports_its_rules_off():
    """Fix 2: no row is no rule, whatever the ownership lists; defaults
    never switch a rule on."""
    from smarter_dev.web.models import AttachmentFilterConfig

    await _switched()
    async with sessions() as session:
        await session.delete(await session.get(AttachmentFilterConfig, GUILD))
        await session.delete(await session.get(ModerationFilterConfig, GUILD))
        await session.commit()
    plan = await _plan()
    doc = plan.document
    assert doc.rule(mp.ATTACHMENT_FILTER).mode == mp.OFF
    assert doc.rule(mp.SPAM_ENGINE).mode == mp.OFF
    assert doc.rule(mp.SPAM_RING).mode == mp.OFF
    assert doc.rule(mp.SCAM_GATE).mode == mp.ENFORCE
    assert sorted(n.split()[0] for n in plan.notes if "row is missing" in n) == [
        mp.ATTACHMENT_FILTER,
        mp.SPAM_ENGINE,
        mp.SPAM_RING,
    ]


def test_classifier_budgets_of_zero_are_refused():
    """Fix 5: a cap of 0 would switch judging off unannounced."""
    for field_name in ("per_author", "per_guild_minute", "per_guild_day"):
        raw = mp.default_document().as_json()
        raw["classifier"][field_name] = 0
        with pytest.raises(mp.PolicyError, match=field_name):
            mp.validate(raw)


def test_the_shadow_keeps_its_own_daily_ceiling_under_a_policy():
    """Fix 6: the policy's daily ceiling is the acting guild's; a guild in
    shadow keeps the shadow's."""
    budget = classifier.ClassifierBudget(per_guild_day=300, acting_per_guild_day=1500)
    limits = classifier.BudgetLimits(
        per_author=2, author_window=600.0, per_guild_minute=20, per_guild_day=900
    )
    assert budget.day_cap(False, limits) == 300
    assert budget.day_cap(True, limits) == 900
    assert budget.day_cap(True) == 1500


async def test_a_setting_saved_by_a_newer_image_does_not_stop_this_one():
    """Fix 7: the bot reads a stored document leniently (an unknown setting,
    a newer schema); a save still refuses unknown keys."""
    from smarter_dev.web.models import ModerationPolicy

    doc = await _imported()
    newer = doc.as_json()
    newer["schema"] = 2
    newer["rules"]["spam_ring"]["new_setting"] = True
    newer["future_section"] = {"x": 1}
    async with sessions() as session:
        row = await session.get(ModerationPolicy, GUILD)
        row.document = newer
        await session.commit()
    async with sessions() as session:
        stored = await store.read_policy(session, GUILD)
    assert stored.document == doc
    assert (await load_policy(GUILD)).revision == 1
    with pytest.raises(mp.PolicyError):
        mp.validate(newer)


async def test_a_stalled_heartbeat_gives_up_and_never_raises(monkeypatch):
    """Fix 4: the loads heartbeat shares the comparison loop; it is bounded."""
    import asyncio

    from smarter_dev.bot.moderation_coordinator import shadow

    started = asyncio.Event()

    async def stalled(_policies, *args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(shadow, "report_loads", stalled)
    monkeypatch.setattr(shadow, "REPORT_LOADS_SECONDS", 0.05)
    await asyncio.wait_for(shadow.heartbeat(PolicyCache()), 5)
    assert started.is_set()
