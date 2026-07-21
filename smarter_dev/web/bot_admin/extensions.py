"""Extension catalog management for the Skrift admin panel.

The admin-facing half of the extension system (design:
``docs/v2/extensions/design.md``). Under
``/admin/bot/guilds/{guild_id}/extensions`` an administrator browses the
in-repo extension catalog, installs an extension into a guild by filling a
config form generated from the extension's declared schema, and later edits the
config, updates to a newer catalog version, enables/disables, or uninstalls.

Every DB mutation is delegated to :mod:`smarter_dev.web.extension_installs`
(slice A) — this controller never touches :class:`AdminHandler` or
:class:`ExtensionInstall` directly. Form reading and config validation are pure
module-level helpers so the accepted-field contract can be unit-tested without a
request or a database, mirroring
:mod:`smarter_dev.web.bot_admin.repeating_messages`. Guild resolution, flash
messaging, and the guild error page are shared with
:mod:`smarter_dev.web.bot_admin.campaigns`.

The module-level :func:`get_registry` call is the fail-fast gate: a malformed
catalog manifest raises :class:`ExtensionRegistryError` at import, so app
startup (which imports this controller via ``app.yaml``) refuses to boot rather
than serving a broken catalog.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Protocol

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Response
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import get_flash_messages
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.extensions.registry import ExtensionRegistryError
from smarter_dev.extensions.registry import LoadedExtension
from smarter_dev.extensions.registry import get_registry
from smarter_dev.extensions.rendering import RenderError
from smarter_dev.extensions.rendering import validate_config_values
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.web.bot_admin.campaigns import fetch_guild_or_error
from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import get_admin_discord_client
from smarter_dev.web.extension_installs import ExtensionConfigOutdatedError
from smarter_dev.web.extension_installs import ExtensionInstall
from smarter_dev.web.extension_installs import ExtensionInstallError
from smarter_dev.web.extension_installs import edit_extension_config
from smarter_dev.web.extension_installs import install_extension
from smarter_dev.web.extension_installs import list_installs
from smarter_dev.web.extension_installs import set_extension_enabled
from smarter_dev.web.extension_installs import uninstall_extension
from smarter_dev.web.extension_installs import update_extension

logger = logging.getLogger(__name__)

_ACTIVE_PAGE = "extensions"
# Fail-fast: a malformed catalog manifest raises here at import time, killing
# app startup with the ExtensionRegistryError message.
_registry = get_registry()


class FormLike(Protocol):
    """The subset of the submitted-form interface the readers rely on.

    Litestar's ``FormMultiDict`` satisfies this; tests can build one directly.
    """

    def get(self, key: str, default: Any = ...) -> Any: ...


def read_extension_config_form(manifest: ExtensionManifest, form: FormLike) -> dict:
    """Extract a submitted config form into a raw field dict.

    Pure function — no I/O. Scalar (``channel_id``/``role_id``/``string``/
    ``int``) fields are read as stripped strings; ``bool`` fields follow the
    checkbox convention (present and equal to ``"true"`` → ``True``, else
    ``False``). The result is suitable both for validation and for re-rendering
    the form after a validation failure.
    """
    raw: dict = {}
    for field in manifest.config:
        if field.type == "bool":
            value = form.get(field.name)
            raw[field.name] = str(value).lower() == "true" if value is not None else False
        else:
            raw[field.name] = (form.get(field.name) or "").strip()
    return raw


def validate_extension_config(
    manifest: ExtensionManifest, raw: dict
) -> tuple[bool, list[str], dict]:
    """Validate a raw config form dict, returning cleaned values.

    Pure function — does **not** mutate ``raw``. Wraps
    :func:`validate_config_values`, translating its :class:`RenderError` into a
    human error list so the form can re-render field-level messages without
    raising. Empty non-bool values are omitted so an optional field falls back
    to its declared default and a missing required field is reported as such.
    Returns ``(is_valid, errors, cleaned)``.
    """
    submitted: dict = {}
    for field in manifest.config:
        value = raw.get(field.name)
        if field.type == "bool":
            submitted[field.name] = bool(value)
        elif isinstance(value, str) and value != "":
            submitted[field.name] = value
    try:
        cleaned = validate_config_values(manifest, submitted)
    except RenderError as exc:
        return False, [str(exc)], {}
    return True, [], cleaned


def _installer_identity(ctx: dict) -> str:
    """The Skrift admin identity to record as ``installed_by`` (best-effort)."""
    user = ctx.get("user")
    identity = getattr(user, "email", None) or getattr(user, "name", None)
    return identity or "admin"


def _initial_values(
    manifest: ExtensionManifest,
    install: ExtensionInstall | None,
    form_data: dict | None,
) -> dict:
    """The per-field value to prefill the config form with.

    Precedence: submitted value (on validation error) → installed config
    (configure mode) → declared default → empty (``""`` / ``False``).
    """
    stored = dict(install.config) if install is not None else {}
    values: dict = {}
    for field in manifest.config:
        if form_data is not None and field.name in form_data:
            values[field.name] = form_data[field.name]
        elif field.name in stored:
            values[field.name] = stored[field.name]
        elif field.default is not None:
            values[field.name] = field.default
        else:
            values[field.name] = False if field.type == "bool" else ""
    return values


async def load_channels_and_roles(guild_id: str) -> tuple[list, list]:
    """Fetch the guild's channels and roles, degrading to empty lists on error.

    A Discord outage must not block the config form — the template falls back to
    text inputs when the lists are empty (mirrors
    :func:`repeating_messages.load_channels_and_roles`, but fetches the full
    channel list because extensions may scope to any channel type).
    """
    client = get_admin_discord_client()
    try:
        channels = await client.get_guild_channels(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch channels for guild %s; using empty list", guild_id
        )
        channels = []
    try:
        roles = await client.get_guild_roles(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch roles for guild %s; using empty list", guild_id
        )
        roles = []
    return channels, roles


class ExtensionsAdminController(Controller):
    """Extension catalog browse + install/config/lifecycle under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- List -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/extensions",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extensions_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Render the catalog with each extension's per-guild install state."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        installs = await list_installs(db_session, guild_id)
        installs_by_slug = {
            install.extension_slug: install for install in installs
        }
        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/extensions/list.html",
            context={
                "guild": guild,
                "guild_id": guild_id,
                "extensions": _registry.all(),
                "installs_by_slug": installs_by_slug,
                "active_page": _ACTIVE_PAGE,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    # -- Install --------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/extensions/{slug:str}/install",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_install_form(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Response:
        """Render the blank config form for installing an extension."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        loaded = _lookup(slug)
        if loaded is None:
            return await _redirect_unknown(request, guild_id, slug)

        return await _render_config_form(
            request, db_session, guild, guild_id, loaded, "install",
            install=None, form_data=None, errors=[],
        )

    @post(
        "/guilds/{guild_id:str}/extensions/{slug:str}/install",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_install(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Response:
        """Validate the config and install the extension, then redirect."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        loaded = _lookup(slug)
        if loaded is None:
            return await _redirect_unknown(request, guild_id, slug)

        form = await request.form()
        raw = read_extension_config_form(loaded.manifest, form)
        is_valid, errors, cleaned = validate_extension_config(loaded.manifest, raw)
        if not is_valid:
            return await _render_config_form(
                request, db_session, guild, guild_id, loaded, "install",
                install=None, form_data=raw, errors=errors, status_code=400,
            )

        ctx = await get_admin_context(request, db_session)
        try:
            await install_extension(
                db_session,
                guild_id=guild_id,
                slug=slug,
                raw_config=cleaned,
                installed_by=_installer_identity(ctx),
            )
        except ExtensionInstallError as exc:
            flash_error(request, str(exc))
            return await _render_config_form(
                request, db_session, guild, guild_id, loaded, "install",
                install=None, form_data=raw, errors=[str(exc)], status_code=400,
            )

        flash_success(request, f"Installed {loaded.manifest.title}.")
        return _redirect_list(guild_id)

    # -- Configure ------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/extensions/{slug:str}/configure",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_configure_form(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Response:
        """Render the config form prefilled from the current install."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        loaded = _lookup(slug)
        if loaded is None:
            return await _redirect_unknown(request, guild_id, slug)

        install = await _find_install(db_session, guild_id, slug)
        if install is None:
            return await _redirect_not_installed(request, guild_id)

        return await _render_config_form(
            request, db_session, guild, guild_id, loaded, "configure",
            install=install, form_data=None, errors=[],
        )

    @post(
        "/guilds/{guild_id:str}/extensions/{slug:str}/configure",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_configure(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Response:
        """Re-render the install's rows with new config (applying any update)."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        loaded = _lookup(slug)
        if loaded is None:
            return await _redirect_unknown(request, guild_id, slug)

        install = await _find_install(db_session, guild_id, slug)
        if install is None:
            return await _redirect_not_installed(request, guild_id)

        form = await request.form()
        raw = read_extension_config_form(loaded.manifest, form)
        is_valid, errors, cleaned = validate_extension_config(loaded.manifest, raw)
        if not is_valid:
            return await _render_config_form(
                request, db_session, guild, guild_id, loaded, "configure",
                install=install, form_data=raw, errors=errors, status_code=400,
            )

        try:
            await edit_extension_config(
                db_session, guild_id=guild_id, slug=slug, raw_config=cleaned
            )
        except ExtensionInstallError as exc:
            flash_error(request, str(exc))
            return await _render_config_form(
                request, db_session, guild, guild_id, loaded, "configure",
                install=install, form_data=raw, errors=[str(exc)], status_code=400,
            )

        flash_success(request, f"Updated {loaded.manifest.title} configuration.")
        return _redirect_list(guild_id)

    # -- Update ---------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/extensions/{slug:str}/update",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_update(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Response:
        """Re-materialise the install at the current catalog version."""
        loaded = _lookup(slug)
        if loaded is None:
            return await _redirect_unknown(request, guild_id, slug)

        try:
            await update_extension(db_session, guild_id=guild_id, slug=slug)
        except ExtensionConfigOutdatedError as exc:
            flash_error(request, str(exc))
            return Redirect(
                path=f"/admin/bot/guilds/{guild_id}/extensions/{slug}/configure"
            )
        except ExtensionInstallError as exc:
            flash_error(request, str(exc))
            return _redirect_list(guild_id)

        flash_success(request, f"Updated {loaded.manifest.title}.")
        return _redirect_list(guild_id)

    # -- Enable / Disable -----------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/extensions/{slug:str}/enable",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_enable(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Redirect:
        """Enable the install and all its handler rows."""
        return await _set_enabled(request, db_session, guild_id, slug, True)

    @post(
        "/guilds/{guild_id:str}/extensions/{slug:str}/disable",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_disable(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Redirect:
        """Disable the install and all its handler rows."""
        return await _set_enabled(request, db_session, guild_id, slug, False)

    # -- Uninstall ------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/extensions/{slug:str}/uninstall",
        guards=[auth_guard, Permission("administrator")],
    )
    async def extension_uninstall(
        self, request: Request, db_session: AsyncSession, guild_id: str, slug: str
    ) -> Redirect:
        """Remove the install and exactly its own handler rows."""
        try:
            await uninstall_extension(db_session, guild_id=guild_id, slug=slug)
        except ExtensionInstallError as exc:
            flash_error(request, str(exc))
            return _redirect_list(guild_id)

        flash_success(request, "Extension uninstalled.")
        return _redirect_list(guild_id)


# -- shared helpers ------------------------------------------------------------


def _lookup(slug: str) -> LoadedExtension | None:
    """The loaded extension for ``slug``, or None if the catalog has no such slug."""
    try:
        return _registry.get(slug)
    except ExtensionRegistryError:
        return None


async def _find_install(
    db_session: AsyncSession, guild_id: str, slug: str
) -> ExtensionInstall | None:
    for install in await list_installs(db_session, guild_id):
        if install.extension_slug == slug:
            return install
    return None


def _redirect_list(guild_id: str) -> Redirect:
    return Redirect(path=f"/admin/bot/guilds/{guild_id}/extensions")


async def _redirect_unknown(request: Request, guild_id: str, slug: str) -> Redirect:
    flash_error(request, f"Unknown extension {slug!r}.")
    return _redirect_list(guild_id)


async def _redirect_not_installed(request: Request, guild_id: str) -> Redirect:
    flash_error(request, "This extension is not installed in this guild.")
    return _redirect_list(guild_id)


async def _set_enabled(
    request: Request,
    db_session: AsyncSession,
    guild_id: str,
    slug: str,
    enabled: bool,
) -> Redirect:
    try:
        await set_extension_enabled(
            db_session, guild_id=guild_id, slug=slug, enabled=enabled
        )
    except ExtensionInstallError as exc:
        flash_error(request, str(exc))
        return _redirect_list(guild_id)

    flash_success(request, "Extension enabled." if enabled else "Extension disabled.")
    return _redirect_list(guild_id)


async def _render_config_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    loaded: LoadedExtension,
    mode: str,
    *,
    install: ExtensionInstall | None,
    form_data: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the shared install/configure config form."""
    channels, roles = await load_channels_and_roles(guild_id)
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/extensions/config_form.html",
        context={
            "guild": guild,
            "guild_id": guild_id,
            "manifest": loaded.manifest,
            "mode": mode,
            "install": install,
            "channels": channels,
            "roles": roles,
            "values": _initial_values(loaded.manifest, install, form_data),
            "errors": errors,
            "active_page": _ACTIVE_PAGE,
            **ctx,
        },
        status_code=status_code,
    )
