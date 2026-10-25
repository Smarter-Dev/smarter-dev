"""#241 PR B: the moderation page, its API, the old forms under a policy, and
the incidents views.

The page is rendered from the real template and its form read back the way
a browser submits it, so "every setting is on the form" and "an unchanged
save changes nothing" are checked against the markup itself.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web import moderation_incidents_view as view
from smarter_dev.web import moderation_policy_store as store
from smarter_dev.web.bot_admin import moderation_policy as page
from smarter_dev.web.bot_admin.guild_configs import GuildConfigsAdminController
from smarter_dev.web.bot_admin.moderation_policy import ModerationPolicyAdminController
from smarter_dev.web.bot_admin_skrift import BotAdminController
from smarter_dev.web.discord_admin_client import DiscordChannel
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.discord_admin_client import DiscordRole
from smarter_dev.web.models import AttachmentFilterConfig
from smarter_dev.web.models import ModerationConfig
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.moderation_gateway import types as gw
from tests.web.admin_template_rendering import render_admin_template

GUILD = "644299523686006834"
REPORTS = "728249959098482829"
GENERAL = "702178352395580001"
MODS = "644390354157568014"
MEMBER = "500000000000000001"
OTHER = "500000000000000002"


def _guild():
    return DiscordGuildDetail(
        id=GUILD,
        name="Smarter Dev",
        icon=None,
        owner_id="1",
        member_count=9,
        description=None,
    )


def _client():
    return SimpleNamespace(
        get_guild=AsyncMock(return_value=_guild()),
        get_guild_channels=AsyncMock(
            return_value=[
                DiscordChannel(id=REPORTS, name="mod-log", type=0, position=0),
                DiscordChannel(id=GENERAL, name="general", type=0, position=1),
            ]
        ),
        get_guild_roles=AsyncMock(
            return_value=[
                DiscordRole(
                    id=MODS,
                    name="mods",
                    color=0,
                    position=3,
                    managed=False,
                    mentionable=True,
                )
            ]
        ),
    )


@contextmanager
def _patched():
    client = _client()
    with (
        patch(
            "smarter_dev.web.bot_admin.squads.get_admin_discord_client",
            return_value=client,
        ),
        patch(
            "smarter_dev.web.bot_admin.squads.get_admin_context",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "smarter_dev.web.bot_admin.guild_configs.get_admin_discord_client",
            return_value=client,
        ),
        patch(f"{page.__name__}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{page.__name__}.get_flash_messages", return_value=[]),
        patch(f"{page.__name__}.flash_error") as error,
        patch(f"{page.__name__}.flash_success") as success,
        patch(
            "smarter_dev.web.bot_admin.guild_configs.get_admin_context",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "smarter_dev.web.bot_admin.guild_configs.get_flash_messages",
            return_value=[],
        ),
    ):
        yield SimpleNamespace(error=error, success=success)


def imported_document() -> mp.PolicyDocument:
    raw = mp.default_document().as_json()
    for rule in mp.RULES:
        raw["rules"][rule]["mode"] = mp.ENFORCE
    raw["rules"]["bump_channel"]["channels"] = ["702178352395583498"]
    raw["rules"]["spam_engine"]["scam_link_domains"] = ["t.me", "wa.me"]
    raw["rules"]["attachment_filter"]["warn_extensions"] = [".py", ".txt"]
    raw["rules"]["attachment_filter"]["delete_message"] = "{user}, removed {extension}"
    raw["protected_role_ids"] = [MODS]
    raw["destinations"].update(report_channel_id=REPORTS, mod_ping_role_id=MODS)
    raw["triage"].update(
        mode=mp.ENFORCE,
        monitored_role_ids=[MODS],
        enabled_tools=["timeout", "purge", "delete"],
        instructions="Freeze, don't judge.\nThen report.",
    )
    return mp.validate(raw)


async def _import(db_session, document=None) -> None:
    await store.save_policy(
        db_session,
        GUILD,
        document or imported_document(),
        base_revision=None,
        by="import",
        source=store.SOURCE_IMPORT,
    )


class _Form(HTMLParser):
    """The fields a browser submits for the page's settings form."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.names: set[str] = set()
        self._in_form = False
        self._textarea: str | None = None
        self._select: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("method") == "post":
            self._in_form = True
        if not self._in_form:
            return
        name = a.get("name")
        if tag == "input" and name:
            self.names.add(name)
            if a.get("type") == "checkbox":
                if "checked" in a:
                    self.fields[name] = "on"
            else:
                self.fields[name] = a.get("value", "")
        elif tag == "textarea" and name:
            self.names.add(name)
            self._textarea, self._text = name, []
        elif tag == "select" and name:
            self.names.add(name)
            self._select = name
        elif tag == "option" and self._select and "selected" in a:
            self.fields[self._select] = a.get("value", "")

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        elif tag == "textarea" and self._textarea:
            self.fields[self._textarea] = "".join(self._text)
            self._textarea = None
        elif tag == "select":
            self._select = None


async def _render(db_session) -> str:
    with _patched():
        response = await ModerationPolicyAdminController.moderation_policy.fn(
            None, request=SimpleNamespace(), db_session=db_session, guild_id=GUILD
        )
    assert response.template_name == "admin/bot/moderation_policy/page.html"
    return render_admin_template(response.template_name, **response.context)


def _form(html: str) -> _Form:
    form = _Form()
    form.feed(html)
    return form


# -- the page and its form ------------------------------------------------------


async def test_every_setting_of_the_policy_is_on_the_form(db_session):
    await _import(db_session)
    form = _form(await _render(db_session))
    wanted = set(page.form_field_names(imported_document().as_json()))
    assert wanted - form.names == set()
    assert "base_revision" in form.names


async def test_saving_the_form_unchanged_writes_the_same_document(db_session):
    await _import(db_session)
    fields = _form(await _render(db_session)).fields
    assert fields["base_revision"] == "1"
    document = page.document_from_form(imported_document().as_json(), fields)
    assert mp.validate(document) == imported_document()


async def test_a_save_writes_the_next_revision_with_what_changed(db_session):
    await _import(db_session)
    fields = _form(await _render(db_session)).fields
    fields["rules.spam_ring.mode"] = mp.EVALUATE
    del fields["sanctions.allowed.ban"]
    del fields["destinations.member_dms"]
    fields["provisional.duration_seconds"] = "120"
    request = SimpleNamespace(form=AsyncMock(return_value=fields))
    with _patched() as flash:
        await ModerationPolicyAdminController.save_moderation_policy.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
    flash.error.assert_not_called()
    stored = await store.read_policy(db_session, GUILD)
    assert stored.revision == 2
    assert stored.document.rules.spam_ring.mode == mp.EVALUATE
    assert stored.document.sanctions.allowed == ["warn", "timeout", "delete"]
    assert stored.document.destinations.member_dms is False
    assert stored.document.provisional.duration_seconds == 120
    [latest, _] = await store.revisions(db_session, GUILD)
    assert set(latest.changed_fields) == {
        "rules.spam_ring.mode",
        "sanctions.allowed",
        "destinations.member_dms",
        "provisional.duration_seconds",
    }


async def test_unticking_everything_saves_empty_lists_not_defaults(db_session):
    await _import(db_session)
    fields = _form(await _render(db_session)).fields
    for name in [
        n
        for n in fields
        if n.startswith(
            ("sanctions.allowed.", "triage.enabled_tools.", "provisional.signals.")
        )
    ]:
        del fields[name]
    request = SimpleNamespace(form=AsyncMock(return_value=fields))
    with _patched():
        await ModerationPolicyAdminController.save_moderation_policy.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
    doc = (await store.read_policy(db_session, GUILD)).document
    assert doc.sanctions.allowed == []
    assert doc.triage.enabled_tools == []
    assert doc.provisional.signals == []


async def test_a_save_from_a_stale_page_is_refused(db_session):
    await _import(db_session)
    fields = _form(await _render(db_session)).fields
    edited = imported_document().as_json()
    edited["rules"]["mass_mention"]["dm"] = False
    await store.save_policy(
        db_session, GUILD, edited, base_revision=1, by="other", source="admin"
    )
    fields["rules.spam_ring.mode"] = mp.OFF
    request = SimpleNamespace(form=AsyncMock(return_value=fields))
    with _patched() as flash:
        await ModerationPolicyAdminController.save_moderation_policy.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
    assert "revision 2, not 1" in flash.error.call_args.args[1]
    stored = await store.read_policy(db_session, GUILD)
    assert stored.revision == 2 and stored.document.rules.spam_ring.mode == mp.ENFORCE


async def test_an_invalid_value_is_refused_by_name(db_session):
    await _import(db_session)
    fields = _form(await _render(db_session)).fields
    fields["destinations.report_channel_id"] = "#mod-log"
    fields["provisional.duration_seconds"] = "soon"
    request = SimpleNamespace(form=AsyncMock(return_value=fields))
    with _patched() as flash:
        await ModerationPolicyAdminController.save_moderation_policy.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
    assert "provisional.duration_seconds" in flash.error.call_args.args[1]
    assert (await store.read_policy(db_session, GUILD)).revision == 1


async def test_without_a_policy_the_page_says_so_and_nothing_saves(db_session):
    html = await _render(db_session)
    assert "No policy yet." in html and "<fieldset disabled" in html
    request = SimpleNamespace(form=AsyncMock(return_value={"base_revision": ""}))
    with _patched() as flash:
        await ModerationPolicyAdminController.save_moderation_policy.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
    assert "no moderation policy yet" in flash.error.call_args.args[1]
    # Nor through the API: only the import creates a policy (it checks the
    # guild is the coordinator's).
    api = SimpleNamespace(
        json=AsyncMock(
            return_value={
                "base_revision": None,
                "document": imported_document().as_json(),
            }
        ),
        session={},
    )
    with _patched():
        created = await ModerationPolicyAdminController.put_policy_json.fn(
            None, request=api, db_session=db_session, guild_id=GUILD
        )
    assert created.status_code == 404
    assert await store.read_policy(db_session, GUILD) is None


async def test_the_page_shows_which_revision_each_process_runs(db_session):
    await _import(db_session)
    await store.save_policy(
        db_session, GUILD, imported_document(), base_revision=1, by="z", source="admin"
    )
    now = datetime.now(UTC)
    await store.record_loads(db_session, "bot/old-pod", {GUILD: (1, now)}, now)
    await store.record_loads(
        db_session,
        "bot/new-pod",
        {GUILD: (2, now - timedelta(minutes=5))},
        now,
    )
    html = await _render(db_session)
    assert "bot/old-pod" in html and "behind" in html
    assert "bot/new-pod" in html and "5 min ago" in html and "current" in html


def test_load_rows_mark_behind_and_quiet():
    now = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    rows = page.load_rows(
        [
            SimpleNamespace(process="a", revision=2, loaded_at=now, seen_at=now),
            SimpleNamespace(process="b", revision=1, loaded_at=now, seen_at=now),
            SimpleNamespace(
                process="c",
                revision=2,
                loaded_at=now,
                seen_at=now - timedelta(minutes=3),
            ),
        ],
        2,
        now,
    )
    assert [(r["behind"], r["quiet"]) for r in rows] == [
        (False, False),
        (True, False),
        (False, True),
    ]


# -- the API ----------------------------------------------------------------------


async def test_the_api_reads_and_writes_on_a_base_revision(db_session):
    await _import(db_session)
    got = await ModerationPolicyAdminController.policy_json.fn(
        None, db_session=db_session, guild_id=GUILD
    )
    assert got.content["revision"] == 1
    document = got.content["document"]
    document["rules"]["edit_ping"]["mode"] = mp.OFF

    def put(body):
        request = SimpleNamespace(json=AsyncMock(return_value=body), session={})
        return ModerationPolicyAdminController.put_policy_json.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )

    with _patched():
        ok = await put({"base_revision": 1, "document": document})
        stale = await put({"base_revision": 1, "document": document})
        bad = await put({"base_revision": 2, "document": {**document, "x": 1}})
        shapeless = await put({"document_is_missing": True})
        baseless = await put({"base_revision": None, "document": document})
    assert ok.content == {"guild_id": GUILD, "revision": 2}
    assert stale.status_code == 409 and stale.content["revision"] == 2
    assert bad.status_code == 422 and bad.content["errors"] == [
        "x: Extra inputs are not permitted"
    ]
    assert shapeless.status_code == 400
    assert baseless.status_code == 400
    assert (
        await store.read_policy(db_session, GUILD)
    ).document.rules.edit_ping.mode == mp.OFF


# -- the old forms under a policy -----------------------------------------------


async def test_the_old_triage_and_attachment_pages_send_you_to_the_policy(db_session):
    await _import(db_session)
    db_session.add(
        ModerationConfig(guild_id=GUILD, is_active=True, enabled_tools=["timeout"])
    )
    await db_session.commit()
    to = f"/admin/bot/guilds/{GUILD}/moderation"
    with _patched():
        triage_get = await BotAdminController.moderation_config.fn(
            None, request=SimpleNamespace(), db_session=db_session, guild_id=GUILD
        )
        triage_post = await BotAdminController.save_moderation_config.fn(
            None,
            request=SimpleNamespace(),
            db_session=db_session,
            guild_id=GUILD,
            data={"is_active": "false", "enabled_tools": ""},
        )
        attachment_get = await GuildConfigsAdminController.attachment_filter_config.fn(
            None, request=SimpleNamespace(), db_session=db_session, guild_id=GUILD
        )
        with patch("smarter_dev.web.bot_admin.guild_configs.flash_error"):
            attachment_post = (
                await GuildConfigsAdminController.save_attachment_filter_config.fn(
                    None,
                    request=SimpleNamespace(
                        form=AsyncMock(return_value={"is_active": "on"})
                    ),
                    db_session=db_session,
                    guild_id=GUILD,
                )
            )
    for response in (triage_get, triage_post, attachment_get, attachment_post):
        assert response.url == to
    row = await db_session.get(ModerationConfig, GUILD)
    assert row.is_active is True and row.enabled_tools == ["timeout"]
    assert await db_session.get(AttachmentFilterConfig, GUILD) is None


async def test_the_old_filter_form_saves_only_the_content_filter(db_session):
    await _import(db_session)
    db_session.add(
        ModerationFilterConfig(
            guild_id=GUILD, spam_engine_enabled=False, staff_exempt_role_ids=[MODS]
        )
    )
    await db_session.commit()
    form = {
        "content_filter_enabled": "on",
        "blocked_tlds": ".zip",
        "spam_engine_enabled": "on",
        "staff_exempt_role_ids": "",
        "message_rate_threshold": "99",
    }
    with _patched(), patch("smarter_dev.web.bot_admin.guild_configs.flash_success"):
        await GuildConfigsAdminController.save_moderation_filter_config.fn(
            None,
            request=SimpleNamespace(form=AsyncMock(return_value=form)),
            db_session=db_session,
            guild_id=GUILD,
        )
    await db_session.refresh(row := await db_session.get(ModerationFilterConfig, GUILD))
    assert row.content_filter_enabled is True and row.blocked_tlds == [".zip"]
    assert row.spam_engine_enabled is False
    assert row.staff_exempt_role_ids == [MODS]
    assert row.message_rate_threshold == 5


# -- incidents --------------------------------------------------------------------


async def _incident(db_session, *, subject=MEMBER, states=(), reported=True):
    incident = ModerationIncident(
        id=uuid4(),
        guild_id=GUILD,
        subject_user_id=subject,
        channel_id=GENERAL,
        active_until=0.0,
        report_channel_id=REPORTS if reported else None,
        report_message_id="900000000000000009" if reported else None,
        report_state="posted" if reported else "none",
    )
    db_session.add(incident)
    await db_session.flush()
    for index, (kind, state, target) in enumerate(states):
        db_session.add(
            ModerationOperation(
                incident_id=incident.id,
                guild_id=GUILD,
                target_user_id=target,
                kind=kind,
                idempotency_key=f"{incident.id}:{index}",
                state=state,
                actor="coordinator",
            )
        )
    db_session.add(
        ModerationIncidentEntry(
            incident_id=incident.id,
            guild_id=GUILD,
            kind=gw.ENTRY_SUMMARY,
            source="coordinator",
            rule="Confirmed scam",
        )
    )
    await db_session.commit()
    return incident


async def test_incidents_count_requested_confirmed_not_applied_and_unknown(db_session):
    incident = await _incident(
        db_session,
        states=[
            (gw.TIMEOUT, gw.COVERED, MEMBER),
            (gw.TIMEOUT, gw.CONFIRMED, MEMBER),
            (gw.DELETE, gw.CONFIRMED, MEMBER),
            (gw.DELETE, gw.SKIPPED, MEMBER),
            (gw.BAN, gw.UNKNOWN, MEMBER),
            (gw.TIMEOUT, gw.FAILED, OTHER),
        ],
    )
    await _incident(db_session, subject=OTHER, reported=False)
    [newest, first] = await view.list_incidents(db_session, GUILD)
    assert first.incident.id == incident.id
    assert first.requested == 6
    assert dict(first.counts) == {
        view.CONFIRMED: 3,
        view.NOT_APPLIED: 2,
        view.UNKNOWN: 1,
    }
    assert first.kinds == [gw.TIMEOUT, gw.DELETE, gw.BAN]
    assert first.summary == "Confirmed scam"
    assert first.report_url == (
        f"https://discord.com/channels/{GUILD}/{REPORTS}/900000000000000009"
    )
    assert newest.report_url is None
    # About OTHER: the incident they are the subject of, and the one where
    # they are a target.
    about_other = await view.list_incidents(db_session, GUILD, user_id=OTHER)
    assert {item.incident.id for item in about_other} == {
        newest.incident.id,
        incident.id,
    }


async def test_the_incident_pages_render_with_their_outcomes(db_session):
    incident = await _incident(
        db_session,
        states=[(gw.TIMEOUT, gw.CONFIRMED, MEMBER), (gw.BAN, gw.UNKNOWN, MEMBER)],
    )
    # What a deleted message was (#378) is shown, not counted away.
    db_session.add(
        ModerationIncidentEntry(
            incident_id=incident.id,
            guild_id=GUILD,
            kind=gw.ENTRY_DELETED_SUMMARY,
            source="coordinator",
            target_user_id=MEMBER,
            content="Fake Steam gift-card giveaway <b>",
            details={"key": "k", "message_ids": ["1"], "status": "ok"},
        )
    )
    await db_session.commit()
    request = SimpleNamespace(query_params={})
    with _patched():
        listing = await ModerationPolicyAdminController.incidents.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
        detail = await ModerationPolicyAdminController.incident.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=GUILD,
            incident_id=str(incident.id),
        )
        missing = await ModerationPolicyAdminController.incident.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id="1",
            incident_id=str(incident.id),
        )
    html = render_admin_template(listing.template_name, **listing.context)
    assert str(incident.id)[:8] in html and "Open in Discord" in html
    html = render_admin_template(detail.template_name, **detail.context)
    assert 'sk-pill-published">confirmed</span>' in html
    assert "unknown" in html and "Confirmed scam" in html
    assert "Fake Steam gift-card giveaway &lt;b&gt;" in html
    assert missing.status_code == 404


def test_history_rows_name_their_incident():
    from smarter_dev.bot.plugins.history import HistoryRow
    from smarter_dev.bot.plugins.history import format_history_row

    row = HistoryRow(
        action_type="timeout",
        occurred_at=datetime(2026, 10, 24, tzinfo=UTC),
        reason="scam",
        source="handler",
        channel_id=None,
        trigger_message_id=None,
        incident_id="4d3f0c64-0000-0000-0000-000000000000",
    )
    assert format_history_row(row, GUILD).endswith("· incident `4d3f0c64`")


class _NoAuthMiddlewareRequest(SimpleNamespace):
    """Like Litestar's request in this app: no auth middleware sets ``user``."""

    @property
    def user(self):
        raise RuntimeError("'user' is not defined in scope")


async def test_a_save_records_the_signed_in_admin(db_session):
    """The app has no auth middleware: the saver comes from the session's
    user (the admin context), never ``request.user``."""
    await _import(db_session)
    fields = _form(await _render(db_session)).fields
    request = _NoAuthMiddlewareRequest(form=AsyncMock(return_value=fields))
    with (
        _patched(),
        patch(
            f"{page.__name__}.get_admin_context",
            new=AsyncMock(return_value={"user": SimpleNamespace(name="Zech")}),
        ),
    ):
        await ModerationPolicyAdminController.save_moderation_policy.fn(
            None, request=request, db_session=db_session, guild_id=GUILD
        )
    stored = await store.read_policy(db_session, GUILD)
    assert stored.revision == 2 and stored.updated_by == "admin:Zech"
