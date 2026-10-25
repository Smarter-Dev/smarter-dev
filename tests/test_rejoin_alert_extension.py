"""Tests for the rejoin-alert catalog extension (version 2, #242).

Four layers:

1. Render/lint layer: the manifest renders against its (empty) example config,
   the rendered script passes ``handler_lint`` and the save-time refusal, and it
   names no mod-log channel any more: the report goes where the moderation
   system reports.
2. Behaviour layer: the rendered script in the real runtime with a recording
   actor: one ``list_mod_actions`` lookup and at most one ``report_for_review``
   per join, nothing at all for a bot join or a clean member.
3. Gateway layer: the rendered script in the real runtime with the real
   :class:`AdminActor`, the real moderation gateway on a test database and the
   fake Discord the gateway tests use: one incident, one evidence note, one
   report in the incident report channel, and no effect (no operation, no
   ``moderation_actions`` row, nothing sent to the member). A second join
   inside the review window reports nothing.
4. Update layer: the live install (version 1, config ``mod_log_channel_id``)
   updates to version 2; the dropped field is discarded and the row gets the
   new script.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import fakeredis.aioredis as fakeredis_aioredis
import pytest
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.extensions.catalog.rejoin_alert import MANIFEST
from smarter_dev.extensions.registry import ExtensionRegistry
from smarter_dev.extensions.registry import LoadedExtension
from smarter_dev.extensions.rendering import RenderedHandler
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.extensions.schema import ConfigField
from smarter_dev.web import extension_installs
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.admin_actions import HandlerFireFacts
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import WindowedLimiter
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.handler_runtime import run_handler_script
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import ModerationOutbox
from tests.moderation_fakes import FakeModerationDiscord

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / (
    "smarter_dev/extensions/catalog/rejoin_alert"
)
_LIVE_V1 = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "live_admin_handlers"
    / "live-rejoin-alert.monty"
)
_HANDLER_KEY = "rejoin-alert"
GUILD = "644299523686006834"
MEMBER = "555000111222333444"
REPORT_CHANNEL = "728249959098482829"
#: The live install's saved config (version 1).
LIVE_CONFIG = {"mod_log_channel_id": "728249959098482829"}
SUMMARY = (
    "Member rejoined — recidivist has 2+ prior mod actions, most recent: ban "
    "on 2026-05-04"
)


def _scripts() -> dict[str, str]:
    return {
        handler.key: (_PACKAGE_DIR / handler.script_file).read_text()
        for handler in MANIFEST.handlers
    }


def _rendered(config: dict | None = None) -> RenderedHandler:
    bundle = render_bundle(MANIFEST, config or MANIFEST.example_config, _scripts())
    return {item.key: item for item in bundle}[_HANDLER_KEY]


def _action_row(
    action_type: str = "ban",
    created_at: str | None = "2026-05-04T18:30:00+00:00",
) -> dict:
    return {
        "action_type": action_type,
        "reason": "spam",
        "source": "manual",
        "moderator_username": "ada",
        "duration_seconds": None,
        "channel_id": "C9",
        "trigger_message_id": "M9",
        "created_at": created_at,
    }


def _join_context(*, is_bot: bool = False, member_id: str = MEMBER) -> dict:
    return {
        "trigger_type": "member_join",
        "member_id": member_id,
        "username": "recidivist",
        "display_name": "Recidivist",
        "is_bot": is_bot,
        "account_created_at": "2021-03-02T00:00:00+00:00",
        "has_custom_avatar": False,
        "guild_member_count": 1200,
        "guild_human_member_count": 1150,
    }


# -- fakes ---------------------------------------------------------------------


@dataclass
class _Emitter:
    messages: list = field(default_factory=list)

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


@dataclass
class _RecordingActor:
    """Stands in for the actor's review report; records each call."""

    reports: list = field(default_factory=list)

    async def report_for_review(self, user_id, summary, *, report_channel_id):
        self.reports.append((user_id, summary, report_channel_id))
        return "REPORT1"


@dataclass
class _ModActionReader:
    rows: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def __call__(self, user_id, limit):
        self.calls.append((user_id, limit))
        return list(self.rows)


def _report_channel(channel: str | None = REPORT_CHANNEL):
    async def read():
        return channel

    return read


async def _run(context: dict, rows: list | None = None):
    emitter = _Emitter()
    actor = _RecordingActor()
    reader = _ModActionReader(rows=rows or [])
    result = await run_handler_script(
        _rendered().script,
        context,
        # member_join is guild-scoped: no triggering channel, exactly as the
        # admin fire job passes it.
        channel_id="",
        guild_id=GUILD,
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget("member_join"),
        actor=actor,
        channel_ids=[],
        memory={},
        mod_action_reader=reader,
        report_channel_reader=_report_channel(),
    )
    return result, emitter, actor, reader


# -- render / manifest layer ---------------------------------------------------


def test_manifest_shape():
    assert MANIFEST.slug == "rejoin-alert"
    assert MANIFEST.version == 2
    assert [handler.key for handler in MANIFEST.handlers] == [_HANDLER_KEY]
    handler = MANIFEST.handlers[0]
    assert handler.trigger_type == "member_join"
    assert handler.channel_scope == []
    assert handler.settings == {}


def test_the_mod_log_channel_field_is_gone():
    """Reports go to the guild's incident report channel; nothing to configure."""
    assert MANIFEST.config == []
    assert MANIFEST.example_config == {}


def test_example_config_renders_and_passes_the_save_time_checks():
    rendered = _rendered()
    assert lint_script(rendered.script) is None
    assert moderation_refusal(rendered.script) is None
    assert rendered.channel_ids == []
    assert "report_for_review(" in rendered.script
    assert "send_message(" not in rendered.script
    assert "MOD_LOG_CHANNEL_ID" not in rendered.script


# -- behaviour layer (rendered script, recording actor) -----------------------


async def test_member_with_history_is_reported_for_review_once():
    result, emitter, actor, reader = await _run(
        _join_context(),
        rows=[
            _action_row("ban", "2026-05-04T18:30:00+00:00"),
            _action_row("warn", "2026-01-09T10:00:00+00:00"),
        ],
    )
    assert result.outcome == "ok", result.error
    assert actor.reports == [(MEMBER, SUMMARY, REPORT_CHANNEL)]
    # The script posts nothing itself.
    assert emitter.messages == []
    assert reader.calls == [(MEMBER, 5)]
    assert result.usage["lookups"] == 1
    assert result.usage["mod_actions"] == 1
    assert result.usage["messages_sent"] == 0


async def test_clean_member_join_is_silent():
    result, emitter, actor, reader = await _run(_join_context(), rows=[])
    assert result.outcome == "ok", result.error
    assert actor.reports == []
    assert emitter.messages == []
    assert len(reader.calls) == 1
    assert result.usage["mod_actions"] == 0


async def test_bot_join_short_circuits_before_the_lookup():
    result, emitter, actor, reader = await _run(
        _join_context(is_bot=True), rows=[_action_row()]
    )
    assert result.outcome == "ok", result.error
    assert reader.calls == []
    assert actor.reports == []
    assert result.usage["lookups"] == 0


async def test_row_without_a_timestamp_still_reports():
    result, _, actor, _ = await _run(_join_context(), rows=[_action_row("kick", None)])
    assert result.outcome == "ok", result.error
    [(_, summary, _)] = actor.reports
    assert "most recent: kick on an unknown date" in summary


async def test_a_full_depth_read_is_still_one_lookup_and_one_report():
    result, _, actor, _ = await _run(
        _join_context(), rows=[_action_row() for _ in range(5)]
    )
    assert result.outcome == "ok", result.error
    assert result.usage["lookups"] == 1
    assert len(actor.reports) == 1
    assert "5+ prior mod actions" in actor.reports[0][1]


# -- gateway layer (real actor, real gateway, fake Discord) --------------------


async def _gateway_fire(
    discord: FakeModerationDiscord,
    limiter: WindowedLimiter,
    *,
    report_channel: str | None = REPORT_CHANNEL,
    member_id: str = MEMBER,
):
    emitter = _Emitter()
    actor = AdminActor(
        bot_token="t",
        guild_id=GUILD,
        staff_exempt_role_ids=(),
        moderation_port=discord,
        fire=HandlerFireFacts(handler_id="H1", handler_name="rejoin-alert"),
    )
    result = await run_handler_script(
        _rendered().script,
        _join_context(member_id=member_id),
        channel_id="",
        guild_id=GUILD,
        emitter=emitter,
        limiter=limiter,
        budget=admin_budget("member_join"),
        actor=actor,
        mod_action_reader=_ModActionReader(rows=[_action_row(), _action_row("warn")]),
        report_channel_reader=_report_channel(report_channel),
    )
    return result, emitter


async def _count(maker, model, *where) -> int:
    async with maker() as session:
        query = select(func.count()).select_from(model)
        for clause in where:
            query = query.where(clause)
        return (await session.execute(query)).scalar_one()


@pytest.fixture
def limiter():
    return WindowedLimiter(redis=fakeredis_aioredis.FakeRedis(decode_responses=True))


async def test_a_rejoin_is_one_review_incident_one_report_and_no_effect(
    moderation_gateway_db, limiter
):
    discord = FakeModerationDiscord(guild_id=GUILD)
    discord.add_member(MEMBER)

    result, emitter = await _gateway_fire(discord, limiter)

    assert result.outcome == "ok", result.error
    maker = await moderation_gateway_db.ensure()
    async with maker() as session:
        incidents = list((await session.execute(select(ModerationIncident))).scalars())
        entries = list(
            (await session.execute(select(ModerationIncidentEntry))).scalars()
        )
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.subject_user_id == MEMBER
    assert incident.report_channel_id == REPORT_CHANNEL
    evidence = [entry for entry in entries if entry.kind == "evidence"]
    assert [(entry.source, entry.content) for entry in evidence] == [
        ("handler:rejoin-alert", SUMMARY)
    ]
    # Review only: nothing was decided, applied or recorded against the member.
    assert await _count(maker, ModerationOperation) == 0
    assert await _count(maker, ModerationAction) == 0
    assert await _count(maker, ModerationOutbox) == 0
    assert discord.calls == []
    assert discord.members == {MEMBER: None}
    # One report, in the incident report channel, naming the member; the
    # script itself sent nothing.
    assert len(discord.posts) == 1
    post = discord.posts[0]
    assert post["channel_id"] == REPORT_CHANNEL
    assert f"<@{MEMBER}>" in post["content"]
    assert SUMMARY in post["content"]
    assert incident.report_message_id == post["message_id"]
    assert emitter.messages == []


async def test_a_join_leave_loop_is_reported_once_per_window(
    moderation_gateway_db, limiter
):
    discord = FakeModerationDiscord(guild_id=GUILD)

    first, _ = await _gateway_fire(discord, limiter)
    again, _ = await _gateway_fire(discord, limiter)
    other, _ = await _gateway_fire(discord, limiter, member_id="555000111222333999")

    assert (first.outcome, again.outcome, other.outcome) == ("ok", "ok", "ok")
    maker = await moderation_gateway_db.ensure()
    # The second join of the same member inside the window: no incident, no
    # report. Another member is reported as usual.
    assert await _count(maker, ModerationIncident) == 2
    assert await _count(
        maker, ModerationIncident, ModerationIncident.subject_user_id == MEMBER
    ) == 1
    assert len(discord.posts) == 2
    assert await _count(maker, ModerationOperation) == 0
    assert await _count(maker, ModerationAction) == 0


async def test_with_reporting_off_the_incident_is_recorded_but_not_reported(
    moderation_gateway_db, limiter
):
    discord = FakeModerationDiscord(guild_id=GUILD)

    result, emitter = await _gateway_fire(discord, limiter, report_channel=None)

    assert result.outcome == "ok", result.error
    maker = await moderation_gateway_db.ensure()
    assert await _count(maker, ModerationIncident) == 1
    assert discord.posts == []
    assert emitter.messages == []


# -- update layer: the live version-1 install moves to version 2 ---------------


def _live_v1_script() -> str:
    """The live row's script (the snapshot adds psql's record separator)."""
    return _LIVE_V1.read_text().removesuffix("\n")


def _version_one() -> LoadedExtension:
    """Version 1 as it was installed: the live script (less the snapshot's
    record separator) with its config placeholder put back, and its one
    config field."""
    template = _live_v1_script().replace(
        '"728249959098482829"', "{{cfg.mod_log_channel_id}}"
    )
    manifest = MANIFEST.model_copy(
        update={
            "version": 1,
            "config": [
                ConfigField(
                    name="mod_log_channel_id", type="channel_id", label="Mod-log"
                )
            ],
            "example_config": dict(LIVE_CONFIG),
        }
    )
    return LoadedExtension(manifest=manifest, scripts={_HANDLER_KEY: template})


def _use_registry(monkeypatch, loaded: LoadedExtension) -> None:
    registry = ExtensionRegistry({loaded.manifest.slug: loaded})
    monkeypatch.setattr(extension_installs, "get_registry", lambda: registry)


async def test_the_live_install_updates_to_the_review_report(db_session, monkeypatch):
    version_one = _version_one()
    _use_registry(monkeypatch, version_one)
    install = await extension_installs.install_extension(
        db_session,
        guild_id=GUILD,
        slug="rejoin-alert",
        raw_config=dict(LIVE_CONFIG),
        installed_by="admin",
    )
    [row] = (
        (
            await db_session.execute(
                select(AdminHandler).where(
                    AdminHandler.extension_install_id == install.id
                )
            )
        )
        .scalars()
        .all()
    )
    # The rendered version 1 is exactly what production runs.
    assert row.script == _live_v1_script()
    assert install.config == LIVE_CONFIG

    # The real catalog: version 2.
    _use_registry(
        monkeypatch, LoadedExtension(manifest=MANIFEST, scripts=_scripts())
    )
    updated = await extension_installs.update_extension(
        db_session, guild_id=GUILD, slug="rejoin-alert"
    )

    await db_session.refresh(row)
    assert updated.installed_version == 2
    assert updated.config == {}
    assert row.script == _rendered().script
    assert "report_for_review(" in row.script
    assert "728249959098482829" not in row.script
    assert row.name == "rejoin-alert"
    assert row.trigger_type == "member_join"
