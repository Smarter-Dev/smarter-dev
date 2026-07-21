"""Install / config-edit / update / enable / disable / uninstall service.

The DB-facing half of the extension system. Every public function takes the
caller's ``AsyncSession``, does its own ``commit()``, and raises
:class:`ExtensionInstallError` (``str(exc)`` is flash-ready) on any domain
failure. The controller never touches :class:`AdminHandler` directly — this
module owns the integrity of extension-owned rows.

Rendering + validation always complete before any DB write (via
:func:`render_bundle`), so a failed render can never leave partial rows. An
install writes the install row and all its handler rows in one transaction; the
only possible orphan is a pre-committed queue job, which the fire path already
no-ops as ``{"status": "missing"}``. Same-install mutators serialise on
``SELECT ... FOR UPDATE`` of the install row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.workers import get_handle
from skrift.workers import submit as worker_submit

from smarter_dev.extensions.registry import (
    ExtensionRegistryError,
    LoadedExtension,
    get_registry,
)
from smarter_dev.extensions.rendering import (
    RenderError,
    RenderedHandler,
    render_bundle,
    validate_config_values,
)
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_caps import MAX_ADMIN_HANDLERS_PER_GUILD
from smarter_dev.web.handler_schedule import ScheduleError, first_fire_at
from smarter_dev.web.models import AdminHandler, ExtensionInstall

logger = logging.getLogger(__name__)

# AdminHandler.created_by_admin is String(20) (a Discord snowflake for bot rows);
# panel-created rows use a literal marker (cf. repeating_messages._CREATED_BY).
# The real Skrift installer identity lives on ExtensionInstall.installed_by.
_CREATED_BY_EXTENSION = "extension"
_TIME_TRIGGERS = ("schedule", "timer")


class ExtensionInstallError(Exception):
    """A domain failure of an extension operation; ``str(exc)`` is flash-ready."""


class ExtensionConfigOutdatedError(ExtensionInstallError):
    """An update's stored config no longer satisfies the newer manifest schema.

    The UI routes the admin to the config form (pre-filled) to supply the new
    required field(s) instead of applying a broken update.
    """


# -- reads ---------------------------------------------------------------------


async def list_installs(session: AsyncSession, guild_id: str) -> list[ExtensionInstall]:
    """Every extension install for a guild, ordered by slug."""
    rows = (
        await session.execute(
            select(ExtensionInstall)
            .where(ExtensionInstall.guild_id == guild_id)
            .order_by(ExtensionInstall.extension_slug)
        )
    ).scalars().all()
    return list(rows)


async def get_install(
    session: AsyncSession, guild_id: str, slug: str
) -> ExtensionInstall | None:
    """The install for (guild, slug), or None."""
    return (
        await session.execute(
            select(ExtensionInstall).where(
                ExtensionInstall.guild_id == guild_id,
                ExtensionInstall.extension_slug == slug,
            )
        )
    ).scalar_one_or_none()


# -- mutators ------------------------------------------------------------------


async def install_extension(
    session: AsyncSession,
    *,
    guild_id: str,
    slug: str,
    raw_config: dict,
    installed_by: str,
) -> ExtensionInstall:
    """Materialise a fresh install: the install row plus one handler row each."""
    loaded = _get_loaded(slug)
    manifest = loaded.manifest
    cleaned = _validate(manifest, raw_config)
    rendered = _render(manifest, cleaned, loaded.scripts)

    if await get_install(session, guild_id, slug) is not None:
        raise ExtensionInstallError(
            "this extension is already installed in this guild"
        )
    await _ensure_capacity(session, guild_id, added=len(rendered))
    await _ensure_names_free(session, guild_id, rendered)

    install = ExtensionInstall(
        guild_id=guild_id,
        extension_slug=slug,
        installed_version=manifest.version,
        config=cleaned,
        enabled=True,
        installed_by=installed_by,
    )
    session.add(install)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ExtensionInstallError(
            "this extension is already installed in this guild"
        ) from exc

    try:
        for item in rendered:
            record = _new_row(install, item, enabled=True)
            session.add(record)
            await session.flush()
            if item.trigger_type in _TIME_TRIGGERS:
                await _arm_time_trigger(record)
        await session.commit()
    except (IntegrityError, ScheduleError) as exc:
        await session.rollback()
        raise ExtensionInstallError(f"could not install extension: {exc}") from exc

    await session.refresh(install)
    return install


async def edit_extension_config(
    session: AsyncSession, *, guild_id: str, slug: str, raw_config: dict
) -> ExtensionInstall:
    """Re-render the install's rows from new config (also advances to the current
    catalog version — the registry only holds the newest templates)."""
    install = await _locked_install(session, guild_id, slug)
    loaded = _get_loaded(slug)
    cleaned = _validate(loaded.manifest, raw_config)
    return await _apply(session, install, loaded, cleaned)


async def update_extension(
    session: AsyncSession, *, guild_id: str, slug: str
) -> ExtensionInstall:
    """Re-materialise the install at the current catalog version using its stored
    config. Raises :class:`ExtensionConfigOutdatedError` when the stored config
    no longer satisfies a newer schema (e.g. a new required field)."""
    install = await _locked_install(session, guild_id, slug)
    loaded = _get_loaded(slug)
    try:
        cleaned = validate_config_values(loaded.manifest, install.config)
    except RenderError as exc:
        raise ExtensionConfigOutdatedError(
            f"this extension's saved config is missing something the new "
            f"version needs: {exc}"
        ) from exc
    return await _apply(session, install, loaded, cleaned)


async def set_extension_enabled(
    session: AsyncSession, *, guild_id: str, slug: str, enabled: bool
) -> ExtensionInstall:
    """Flip the install and every owned row's ``enabled``; cancel/re-arm schedules."""
    install = await _locked_install(session, guild_id, slug)
    install.enabled = enabled
    for row in await _owned_rows(session, install):
        row.enabled = enabled
        if enabled:
            if row.trigger_type in _TIME_TRIGGERS:
                await _cancel_scheduled_job(row)
                await _arm_time_trigger(row)
        else:
            await _cancel_scheduled_job(row)
    install.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(install)
    return install


async def uninstall_extension(
    session: AsyncSession, *, guild_id: str, slug: str
) -> None:
    """Delete the install and exactly its own handler rows (cancelling schedules)."""
    install = await _locked_install(session, guild_id, slug)
    for row in await _owned_rows(session, install):
        await _cancel_scheduled_job(row)
        await session.delete(row)
    await session.delete(install)
    await session.commit()


# -- shared internals ----------------------------------------------------------


async def _apply(
    session: AsyncSession,
    install: ExtensionInstall,
    loaded: LoadedExtension,
    cleaned: dict,
) -> ExtensionInstall:
    """Render + reconcile rows for a new config, then advance the install."""
    rendered = _render(loaded.manifest, cleaned, loaded.scripts)
    try:
        await _sync_handler_rows(session, install, loaded, rendered)
        install.config = cleaned
        install.installed_version = loaded.manifest.version
        install.updated_at = datetime.now(timezone.utc)
        await session.commit()
    except (IntegrityError, ScheduleError) as exc:
        await session.rollback()
        raise ExtensionInstallError(f"could not apply config: {exc}") from exc
    await session.refresh(install)
    return install


async def _sync_handler_rows(
    session: AsyncSession,
    install: ExtensionInstall,
    loaded: LoadedExtension,
    rendered: list[RenderedHandler],
) -> None:
    """Reconcile owned rows to ``rendered``, matching by handler key.

    Existing keys are updated in place (row id and ``memory`` preserved); a key
    whose trigger_type changed is delete+insert (immutable trigger type); keys
    absent from the render are deleted; new keys are inserted. Each row's
    ``enabled`` is set to the install's, since the toggle owns enablement.
    """
    existing = await _owned_rows(session, install)
    by_key = {row.extension_handler_key: row for row in existing}

    final_total = (
        await _guild_handler_count(session, install.guild_id)
        - len(existing)
        + len(rendered)
    )
    if final_total > MAX_ADMIN_HANDLERS_PER_GUILD:
        raise ExtensionInstallError(
            f"this change would exceed the {MAX_ADMIN_HANDLERS_PER_GUILD}-handler "
            "limit for this guild"
        )

    # Remove stale keys and trigger-type-changed rows first, then flush so a
    # same-name reinsert does not race the unique index.
    for key, row in by_key.items():
        item = next((r for r in rendered if r.key == key), None)
        if item is None or item.trigger_type != row.trigger_type:
            await _cancel_scheduled_job(row)
            await session.delete(row)
    await session.flush()

    await _ensure_names_free(session, install.guild_id, rendered, install_id=install.id)

    for item in rendered:
        row = by_key.get(item.key)
        if row is not None and row.trigger_type == item.trigger_type:
            row.name = item.name
            row.description = item.description
            row.script = item.script
            row.settings = item.settings
            row.channel_ids = item.channel_ids
            row.enabled = install.enabled
            if item.trigger_type in _TIME_TRIGGERS:
                await _cancel_scheduled_job(row)
                if install.enabled:
                    await _arm_time_trigger(row)
        else:
            record = _new_row(install, item, enabled=install.enabled)
            session.add(record)
            await session.flush()
            if item.trigger_type in _TIME_TRIGGERS and install.enabled:
                await _arm_time_trigger(record)


async def _locked_install(
    session: AsyncSession, guild_id: str, slug: str
) -> ExtensionInstall:
    install = (
        await session.execute(
            select(ExtensionInstall)
            .where(
                ExtensionInstall.guild_id == guild_id,
                ExtensionInstall.extension_slug == slug,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if install is None:
        raise ExtensionInstallError(
            "this extension is not installed in this guild"
        )
    return install


async def _owned_rows(
    session: AsyncSession, install: ExtensionInstall
) -> list[AdminHandler]:
    rows = (
        await session.execute(
            select(AdminHandler).where(
                AdminHandler.extension_install_id == install.id
            )
        )
    ).scalars().all()
    return list(rows)


async def _guild_handler_count(session: AsyncSession, guild_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AdminHandler)
            .where(AdminHandler.guild_id == guild_id)
        )
    ).scalar_one()


async def _ensure_capacity(
    session: AsyncSession, guild_id: str, *, added: int
) -> None:
    count = await _guild_handler_count(session, guild_id)
    if count + added > MAX_ADMIN_HANDLERS_PER_GUILD:
        raise ExtensionInstallError(
            f"installing this extension would exceed the "
            f"{MAX_ADMIN_HANDLERS_PER_GUILD}-handler limit for this guild"
        )


async def _ensure_names_free(
    session: AsyncSession,
    guild_id: str,
    rendered: list[RenderedHandler],
    *,
    install_id=None,
) -> None:
    """Fail if any rendered name is taken by a row this install does not own."""
    for item in rendered:
        query = select(AdminHandler.id).where(
            AdminHandler.guild_id == guild_id, AdminHandler.name == item.name
        )
        if install_id is not None:
            query = query.where(
                (AdminHandler.extension_install_id != install_id)
                | (AdminHandler.extension_install_id.is_(None))
            )
        if (await session.execute(query)).first() is not None:
            raise ExtensionInstallError(
                f"rename or delete the existing admin handler '{item.name}' first"
            )


def _new_row(
    install: ExtensionInstall, item: RenderedHandler, *, enabled: bool
) -> AdminHandler:
    return AdminHandler(
        guild_id=install.guild_id,
        name=item.name,
        trigger_type=item.trigger_type,
        settings=item.settings,
        channel_ids=item.channel_ids,
        description=item.description,
        script=item.script,
        created_by_admin=_CREATED_BY_EXTENSION,
        enabled=enabled,
        extension_install_id=install.id,
        extension_handler_key=item.key,
    )


async def _cancel_scheduled_job(record: AdminHandler) -> None:
    """Best-effort cancel of a row's pending fire (mirrors admin_handlers._reschedule)."""
    if record.scheduled_job_id:
        try:
            await get_handle(record.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001 — best-effort; the chain also self-stops
            logger.warning("could not cancel job %s", record.scheduled_job_id)
        record.scheduled_job_id = None


async def _arm_time_trigger(record: AdminHandler) -> None:
    """Schedule the first fire from a row's current settings (mirrors _reschedule)."""
    fire_at = first_fire_at(
        record.trigger_type, record.settings or {}, datetime.now(timezone.utc)
    )
    job_id = uuid4().hex
    await worker_submit(
        AdminHandlerFirePayload(
            admin_handler_id=str(record.id),
            channel_id=(record.channel_ids[0] if record.channel_ids else ""),
            trigger_context={"trigger_type": record.trigger_type},
        ),
        scheduled_for=fire_at,
        job_id=job_id,
    )
    record.scheduled_job_id = job_id


def _get_loaded(slug: str) -> LoadedExtension:
    try:
        return get_registry().get(slug)
    except ExtensionRegistryError as exc:
        raise ExtensionInstallError(f"unknown extension {slug!r}") from exc


def _validate(manifest: ExtensionManifest, raw_config: dict) -> dict:
    try:
        return validate_config_values(manifest, raw_config)
    except RenderError as exc:
        raise ExtensionInstallError(str(exc)) from exc


def _render(
    manifest: ExtensionManifest, cleaned: dict, scripts: dict[str, str]
) -> list[RenderedHandler]:
    try:
        return render_bundle(manifest, cleaned, scripts)
    except RenderError as exc:
        raise ExtensionInstallError(str(exc)) from exc
