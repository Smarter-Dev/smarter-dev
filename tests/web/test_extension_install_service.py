"""DB tests for the extension install service.

Exercises install / config-edit / update / enable-disable / uninstall against
the SQLite test database, with the worker seams stubbed and the registry
swapped for synthetic test extensions (so schedule handlers and multiple
catalog versions can be driven). Verifies row ownership, atomicity, schedule
arming/cancelling, and that hand-authored handlers are never touched.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smarter_dev.extensions.registry import ExtensionRegistry, LoadedExtension
from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)
from smarter_dev.web import extension_installs as svc
from smarter_dev.web.extension_installs import (
    ExtensionConfigOutdatedError,
    ExtensionInstallError,
    edit_extension_config,
    get_install,
    install_extension,
    list_installs,
    set_extension_enabled,
    uninstall_extension,
    update_extension,
)
from smarter_dev.web.models import AdminHandler, ExtensionInstall, HandlerRun

GUILD = "111111111111111111"
CHAN_A = "123456789012345678"
CHAN_B = "876543210987654321"


# -- fixtures ------------------------------------------------------------------


@pytest.fixture
def worker_stub(monkeypatch):
    submitted: list = []
    cancelled: list = []

    async def _submit(payload, **kwargs):
        submitted.append((payload, kwargs))

    class _Handle:
        def __init__(self, job_id):
            self.job_id = job_id

        async def cancel(self):
            cancelled.append(self.job_id)

    monkeypatch.setattr(svc, "worker_submit", _submit)
    monkeypatch.setattr(svc, "get_handle", _Handle)
    return types.SimpleNamespace(submitted=submitted, cancelled=cancelled)


def _loaded(manifest: ExtensionManifest, scripts: dict[str, str]) -> LoadedExtension:
    return LoadedExtension(manifest=manifest, scripts=scripts)


def _use_registry(monkeypatch, *loaded_exts: LoadedExtension) -> None:
    registry = ExtensionRegistry({e.manifest.slug: e for e in loaded_exts})
    monkeypatch.setattr(svc, "get_registry", lambda: registry)


# -- manifest builders ---------------------------------------------------------

_MIRROR_SCRIPT = "send_message('hello', {{cfg.chan}})\n"
_DIGEST_SCRIPT = "pass\n"


def _relay(version: int = 1) -> LoadedExtension:
    """A 2-handler bundle: a channel-scoped message handler + a schedule handler."""
    manifest = ExtensionManifest(
        slug="test-relay",
        title="Relay",
        summary="S",
        version=version,
        config=[
            ConfigField(name="chan", type="channel_id", label="C"),
            ConfigField(
                name="every", type="int", label="E", required=False, default=120
            ),
        ],
        handlers=[
            HandlerTemplate(
                key="mirror",
                name="test-mirror",
                trigger_type="message",
                description="mirror",
                script_file="mirror.monty",
                channel_scope=["chan"],
            ),
            HandlerTemplate(
                key="digest",
                name="test-digest",
                trigger_type="schedule",
                description="digest",
                script_file="digest.monty",
                settings={"interval_seconds": "{{cfg.every}}"},
            ),
        ],
        example_config={"chan": CHAN_A, "every": 120},
    )
    return _loaded(manifest, {"mirror": _MIRROR_SCRIPT, "digest": _DIGEST_SCRIPT})


async def _owned(session, install_id) -> list[AdminHandler]:
    rows = (
        await session.execute(
            select(AdminHandler).where(
                AdminHandler.extension_install_id == install_id
            )
        )
    ).scalars().all()
    return list(rows)


async def _count_handlers(session, guild_id) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AdminHandler)
            .where(AdminHandler.guild_id == guild_id)
        )
    ).scalar_one()


# -- install -------------------------------------------------------------------


async def test_install_happy_path(db_session, worker_stub, monkeypatch):
    _use_registry(monkeypatch, _relay())
    install = await install_extension(
        db_session,
        guild_id=GUILD,
        slug="test-relay",
        raw_config={"chan": CHAN_A, "every": "120"},
        installed_by="admin@example.com",
    )
    assert install.installed_version == 1
    assert install.config == {"chan": CHAN_A, "every": 120}
    assert install.enabled is True
    assert install.installed_by == "admin@example.com"

    rows = await _owned(db_session, install.id)
    assert {r.extension_handler_key for r in rows} == {"mirror", "digest"}
    assert all(r.created_by_admin == "extension" for r in rows)
    assert all(r.enabled for r in rows)

    mirror = next(r for r in rows if r.extension_handler_key == "mirror")
    assert mirror.channel_ids == [CHAN_A]
    assert f'"{CHAN_A}"' in mirror.script

    digest = next(r for r in rows if r.extension_handler_key == "digest")
    assert digest.scheduled_job_id is not None
    assert digest.settings == {"interval_seconds": 120}
    assert len(worker_stub.submitted) == 1


async def test_install_atomic_on_second_handler_name_collision(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay())
    # A hand-authored row already owns the SECOND handler's name.
    db_session.add(
        AdminHandler(
            guild_id=GUILD,
            name="test-digest",
            trigger_type="message",
            settings={},
            channel_ids=[],
            description="hand authored",
            script="pass\n",
            created_by_admin="999",
        )
    )
    await db_session.commit()

    with pytest.raises(ExtensionInstallError, match="test-digest"):
        await install_extension(
            db_session,
            guild_id=GUILD,
            slug="test-relay",
            raw_config={"chan": CHAN_A},
            installed_by="admin",
        )

    assert (await db_session.execute(select(func.count()).select_from(ExtensionInstall))).scalar_one() == 0
    # Only the pre-existing hand-authored row remains; no extension rows written.
    assert await _count_handlers(db_session, GUILD) == 1
    assert len(worker_stub.submitted) == 0


async def test_double_install_rejected(db_session, worker_stub, monkeypatch):
    _use_registry(monkeypatch, _relay())
    await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    with pytest.raises(ExtensionInstallError, match="already installed"):
        await install_extension(
            db_session, guild_id=GUILD, slug="test-relay",
            raw_config={"chan": CHAN_A}, installed_by="admin",
        )


async def test_guild_cap_blocks_install(db_session, worker_stub, monkeypatch):
    _use_registry(monkeypatch, _relay())
    for i in range(19):
        db_session.add(
            AdminHandler(
                guild_id=GUILD,
                name=f"hand-{i}",
                trigger_type="message",
                settings={},
                channel_ids=[],
                description="d",
                script="pass\n",
                created_by_admin="999",
            )
        )
    await db_session.commit()

    with pytest.raises(ExtensionInstallError, match="limit"):
        await install_extension(
            db_session, guild_id=GUILD, slug="test-relay",
            raw_config={"chan": CHAN_A}, installed_by="admin",
        )
    assert (await db_session.execute(select(func.count()).select_from(ExtensionInstall))).scalar_one() == 0
    assert await _count_handlers(db_session, GUILD) == 19


async def test_unknown_slug_rejected(db_session, worker_stub, monkeypatch):
    _use_registry(monkeypatch, _relay())
    with pytest.raises(ExtensionInstallError, match="unknown extension"):
        await install_extension(
            db_session, guild_id=GUILD, slug="nope",
            raw_config={}, installed_by="admin",
        )


# -- config edit ---------------------------------------------------------------


async def test_edit_config_updates_rows_in_place(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay())
    install = await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    rows_before = await _owned(db_session, install.id)
    ids_before = {r.extension_handler_key: r.id for r in rows_before}
    mirror = next(r for r in rows_before if r.extension_handler_key == "mirror")
    mirror.memory = {"seen": 7}
    old_digest_job = next(
        r for r in rows_before if r.extension_handler_key == "digest"
    ).scheduled_job_id
    await db_session.commit()

    updated = await edit_extension_config(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_B, "every": "300"},
    )
    assert updated.config == {"chan": CHAN_B, "every": 300}

    rows_after = await _owned(db_session, install.id)
    ids_after = {r.extension_handler_key: r.id for r in rows_after}
    assert ids_after == ids_before  # rows reused, not recreated

    mirror_after = next(r for r in rows_after if r.extension_handler_key == "mirror")
    assert mirror_after.memory == {"seen": 7}  # per-handler state preserved
    assert mirror_after.channel_ids == [CHAN_B]
    assert f'"{CHAN_B}"' in mirror_after.script

    digest_after = next(r for r in rows_after if r.extension_handler_key == "digest")
    assert digest_after.settings == {"interval_seconds": 300}
    assert old_digest_job in worker_stub.cancelled
    assert digest_after.scheduled_job_id not in (None, old_digest_job)


# -- update --------------------------------------------------------------------


def _relay_v2_reshaped() -> LoadedExtension:
    """v2: keep-in-place, a removed handler, a trigger-type change, a new handler."""
    manifest = ExtensionManifest(
        slug="reshape",
        title="R",
        summary="S",
        version=2,
        config=[ConfigField(name="chan", type="channel_id", label="C")],
        handlers=[
            HandlerTemplate(
                key="keep",
                name="rs-keep",
                trigger_type="message",
                description="keep v2",
                script_file="keep.monty",
                channel_scope=["chan"],
            ),
            HandlerTemplate(
                key="morph",
                name="rs-morph",
                trigger_type="schedule",  # was message in v1
                description="morph",
                script_file="morph.monty",
                settings={"interval_seconds": 120},
            ),
            HandlerTemplate(
                key="added",
                name="rs-added",
                trigger_type="message",
                description="added",
                script_file="added.monty",
            ),
        ],
        example_config={"chan": CHAN_A},
    )
    return _loaded(
        manifest,
        {"keep": "pass\n", "morph": "pass\n", "added": "pass\n"},
    )


def _relay_v1_reshaped() -> LoadedExtension:
    manifest = ExtensionManifest(
        slug="reshape",
        title="R",
        summary="S",
        version=1,
        config=[ConfigField(name="chan", type="channel_id", label="C")],
        handlers=[
            HandlerTemplate(
                key="keep",
                name="rs-keep",
                trigger_type="message",
                description="keep v1",
                script_file="keep.monty",
                channel_scope=["chan"],
            ),
            HandlerTemplate(
                key="gone",
                name="rs-gone",
                trigger_type="schedule",
                description="gone",
                script_file="gone.monty",
                settings={"interval_seconds": 120},
            ),
            HandlerTemplate(
                key="morph",
                name="rs-morph",
                trigger_type="message",
                description="morph v1",
                script_file="morph.monty",
            ),
        ],
        example_config={"chan": CHAN_A},
    )
    return _loaded(
        manifest,
        {"keep": "pass\n", "gone": "pass\n", "morph": "pass\n"},
    )


async def test_update_reconciles_add_remove_and_trigger_change(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay_v1_reshaped())
    install = await install_extension(
        db_session, guild_id=GUILD, slug="reshape",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    before = {r.extension_handler_key: r for r in await _owned(db_session, install.id)}
    keep_id = before["keep"].id
    morph_id_v1 = before["morph"].id
    gone_job = before["gone"].scheduled_job_id

    # A hand-authored row in the same guild must be left completely alone.
    db_session.add(
        AdminHandler(
            guild_id=GUILD, name="hand", trigger_type="message", settings={},
            channel_ids=[], description="d", script="pass\n", created_by_admin="9",
        )
    )
    await db_session.commit()

    _use_registry(monkeypatch, _relay_v2_reshaped())
    updated = await update_extension(db_session, guild_id=GUILD, slug="reshape")
    assert updated.installed_version == 2

    after = {r.extension_handler_key: r for r in await _owned(db_session, install.id)}
    assert set(after) == {"keep", "morph", "added"}
    assert after["keep"].id == keep_id  # in-place
    assert after["keep"].description == "keep v2"
    assert gone_job in worker_stub.cancelled  # removed handler's job cancelled
    # trigger-type change => delete + insert (new row id), now a schedule.
    assert after["morph"].id != morph_id_v1
    assert after["morph"].trigger_type == "schedule"
    assert after["morph"].scheduled_job_id is not None

    hand = (
        await db_session.execute(
            select(AdminHandler).where(AdminHandler.name == "hand")
        )
    ).scalar_one()
    assert hand.extension_install_id is None


def _relay_v2_new_required() -> LoadedExtension:
    manifest = ExtensionManifest(
        slug="test-relay",
        title="Relay",
        summary="S",
        version=2,
        config=[
            ConfigField(name="chan", type="channel_id", label="C"),
            ConfigField(
                name="every", type="int", label="E", required=False, default=120
            ),
            ConfigField(name="newthing", type="string", label="N"),  # new required
        ],
        handlers=[
            HandlerTemplate(
                key="mirror",
                name="test-mirror",
                trigger_type="message",
                description="mirror",
                script_file="mirror.monty",
                channel_scope=["chan"],
            ),
            HandlerTemplate(
                key="digest",
                name="test-digest",
                trigger_type="schedule",
                description="digest",
                script_file="digest.monty",
                settings={"interval_seconds": "{{cfg.every}}"},
            ),
        ],
        example_config={"chan": CHAN_A, "every": 120, "newthing": "x"},
    )
    return _loaded(manifest, {"mirror": _MIRROR_SCRIPT, "digest": _DIGEST_SCRIPT})


async def test_update_with_new_required_field_raises_and_no_changes(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay())
    install = await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    keys_before = {r.extension_handler_key for r in await _owned(db_session, install.id)}

    _use_registry(monkeypatch, _relay_v2_new_required())
    with pytest.raises(ExtensionConfigOutdatedError):
        await update_extension(db_session, guild_id=GUILD, slug="test-relay")

    await db_session.rollback()
    refreshed = await get_install(db_session, GUILD, "test-relay")
    assert refreshed.installed_version == 1
    keys_after = {r.extension_handler_key for r in await _owned(db_session, install.id)}
    assert keys_after == keys_before


# -- enable / disable ----------------------------------------------------------


async def test_disable_then_enable_flips_rows_and_schedule(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay())
    install = await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    digest = next(
        r for r in await _owned(db_session, install.id)
        if r.extension_handler_key == "digest"
    )
    armed_job = digest.scheduled_job_id

    disabled = await set_extension_enabled(
        db_session, guild_id=GUILD, slug="test-relay", enabled=False
    )
    assert disabled.enabled is False
    rows = await _owned(db_session, install.id)
    assert all(not r.enabled for r in rows)
    digest = next(r for r in rows if r.extension_handler_key == "digest")
    assert digest.scheduled_job_id is None
    assert armed_job in worker_stub.cancelled

    enabled = await set_extension_enabled(
        db_session, guild_id=GUILD, slug="test-relay", enabled=True
    )
    assert enabled.enabled is True
    rows = await _owned(db_session, install.id)
    assert all(r.enabled for r in rows)
    digest = next(r for r in rows if r.extension_handler_key == "digest")
    assert digest.scheduled_job_id is not None


async def test_redundant_enable_cancels_prior_job_before_rearming(
    db_session, worker_stub, monkeypatch
):
    """A redundant enable on an already-enabled install must cancel the live
    schedule job before arming a new one, so no orphaned self-rescheduling
    fire chain is left running alongside the new one."""
    _use_registry(monkeypatch, _relay())
    install = await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    digest = next(
        r for r in await _owned(db_session, install.id)
        if r.extension_handler_key == "digest"
    )
    original_job = digest.scheduled_job_id
    assert original_job is not None

    # Redundant enable while still enabled (double-clicked / retried Enable POST).
    await set_extension_enabled(
        db_session, guild_id=GUILD, slug="test-relay", enabled=True
    )

    digest = next(
        r for r in await _owned(db_session, install.id)
        if r.extension_handler_key == "digest"
    )
    # The prior live job was cancelled, and a fresh distinct job now owns the row.
    assert original_job in worker_stub.cancelled
    assert digest.scheduled_job_id not in (None, original_job)


async def test_edit_config_while_disabled_keeps_rows_disabled(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay())
    await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    await set_extension_enabled(
        db_session, guild_id=GUILD, slug="test-relay", enabled=False
    )
    install = await edit_extension_config(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_B},
    )
    rows = await _owned(db_session, install.id)
    assert all(not r.enabled for r in rows)


# -- uninstall -----------------------------------------------------------------


async def test_uninstall_removes_only_owned_rows(
    db_session, worker_stub, monkeypatch
):
    _use_registry(monkeypatch, _relay())
    install = await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    owned = await _owned(db_session, install.id)
    mirror_id = next(r.id for r in owned if r.extension_handler_key == "mirror")
    digest_job = next(
        r.scheduled_job_id for r in owned if r.extension_handler_key == "digest"
    )

    # An audit run for an owned row, plus a hand-authored row, must both survive.
    db_session.add(HandlerRun(handler_id=mirror_id, handler_kind="admin", outcome="ok"))
    db_session.add(
        AdminHandler(
            guild_id=GUILD, name="hand", trigger_type="message", settings={},
            channel_ids=[], description="d", script="pass\n", created_by_admin="9",
        )
    )
    await db_session.commit()

    await uninstall_extension(db_session, guild_id=GUILD, slug="test-relay")

    assert await get_install(db_session, GUILD, "test-relay") is None
    assert await _owned(db_session, install.id) == []
    assert digest_job in worker_stub.cancelled
    # Hand-authored row intact.
    assert await _count_handlers(db_session, GUILD) == 1
    # Audit run retained (no FK cascade).
    run_count = (
        await db_session.execute(
            select(func.count()).select_from(HandlerRun).where(
                HandlerRun.handler_id == mirror_id
            )
        )
    ).scalar_one()
    assert run_count == 1


async def test_list_installs_scoped_to_guild(db_session, worker_stub, monkeypatch):
    _use_registry(monkeypatch, _relay())
    await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    await install_extension(
        db_session, guild_id="222222222222222222", slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="admin",
    )
    installs = await list_installs(db_session, GUILD)
    assert [i.guild_id for i in installs] == [GUILD]


# -- cross-session install race ------------------------------------------------


async def test_cross_session_double_install_leaves_one(
    db_session, worker_stub, monkeypatch, test_engine
):
    """A second session installing the same (guild, slug) loses on the unique
    constraint — exactly one install and one handler bundle survive.

    (SQLite's StaticPool serialises connections, so this drives the
    cross-session unique-index path rather than true parallelism.)
    """
    _use_registry(monkeypatch, _relay())
    await install_extension(
        db_session, guild_id=GUILD, slug="test-relay",
        raw_config={"chan": CHAN_A}, installed_by="first",
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as other:
        with pytest.raises(ExtensionInstallError):
            await install_extension(
                other, guild_id=GUILD, slug="test-relay",
                raw_config={"chan": CHAN_A}, installed_by="second",
            )

    installs = (
        await db_session.execute(select(func.count()).select_from(ExtensionInstall))
    ).scalar_one()
    assert installs == 1
    assert await _count_handlers(db_session, GUILD) == 2
