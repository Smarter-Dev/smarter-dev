"""Tests for the Skrift admin extensions controller (slice B)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from litestar.datastructures import FormMultiDict
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard

from smarter_dev.extensions.registry import get_registry
from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate
from smarter_dev.web.bot_admin.extensions import ExtensionsAdminController
from smarter_dev.web.bot_admin.extensions import read_extension_config_form
from smarter_dev.web.bot_admin.extensions import validate_extension_config
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.discord_admin_client import GuildNotFoundError
from smarter_dev.web.extension_installs import get_install
from smarter_dev.web.extension_installs import list_installs
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ExtensionInstall

_GUILD = "111111111111111111"
_SLUG = "dm-forum-relay"
_FORUM = "123456789012345678"
_STAFF = "234567890123456789"
_MODULE = "smarter_dev.web.bot_admin.extensions"
# Guild resolution is shared with the campaigns module, so the Discord client is
# patched where the shared helper actually looks it up.
_CAMPAIGNS_MODULE = "smarter_dev.web.bot_admin.campaigns"


def _manifest():
    return get_registry().get(_SLUG).manifest


def _guild_detail() -> DiscordGuildDetail:
    return DiscordGuildDetail(
        id=_GUILD,
        name="Alpha Guild",
        icon=None,
        owner_id="owner",
        member_count=42,
        description=None,
    )


def _admin_client() -> SimpleNamespace:
    return SimpleNamespace(
        get_guild=AsyncMock(return_value=_guild_detail()),
        get_guild_channels=AsyncMock(
            return_value=[SimpleNamespace(id=_FORUM, name="staff-forum")]
        ),
        get_guild_roles=AsyncMock(
            return_value=[SimpleNamespace(id=_STAFF, name="Staff")]
        ),
    )


def _ctx() -> dict:
    return {"user": SimpleNamespace(email="admin@example.com", name="Admin")}


def _form(pairs: list[tuple[str, str]]) -> FormMultiDict:
    return FormMultiDict(pairs)


def _valid_form_pairs() -> list[tuple[str, str]]:
    return [
        ("forum_channel_id", _FORUM),
        ("notify_on_first_dm", "true"),
    ]


def _manifest_with_optional_string() -> ExtensionManifest:
    """A fixture manifest with an optional defaulted string field.

    The catalog's dm-forum-relay manifest has no optional string field, so the
    blank-optional-falls-back-to-default contract is exercised against this
    purpose-built manifest instead of a live catalog entry.
    """
    return ExtensionManifest(
        slug="optional-default-fixture",
        title="Optional default fixture",
        summary="Exercises the blank-optional-falls-back-to-default path.",
        version=1,
        config=[
            ConfigField(
                name="target_channel_id", type="channel_id", label="Channel"
            ),
            ConfigField(
                name="footer_text",
                type="string",
                label="Footer",
                required=False,
                default="— relayed by staff",
            ),
        ],
        handlers=[
            HandlerTemplate(
                key="fixture",
                name="fixture-handler",
                trigger_type="message",
                description="fixture handler",
                script_file="fixture.monty",
            )
        ],
        example_config={"target_channel_id": _FORUM},
    )


# --- pure: read_extension_config_form ----------------------------------------


def test_read_form_strips_scalars_and_reads_bool_checkbox():
    raw = read_extension_config_form(
        _manifest(),
        _form(
            [
                ("forum_channel_id", f"  {_FORUM}  "),
                ("notify_on_first_dm", "true"),
            ]
        ),
    )
    assert raw["forum_channel_id"] == _FORUM
    assert raw["notify_on_first_dm"] is True


def test_read_form_absent_checkbox_is_false():
    raw = read_extension_config_form(
        _manifest(),
        _form([("forum_channel_id", _FORUM)]),
    )
    assert raw["notify_on_first_dm"] is False
    assert raw["forum_channel_id"] == _FORUM


# --- pure: validate_extension_config -----------------------------------------


def test_validate_happy_path_types_and_defaults_optional():
    raw = read_extension_config_form(_manifest(), _form(_valid_form_pairs()))
    ok, errors, cleaned = validate_extension_config(_manifest(), raw)
    assert ok is True
    assert errors == []
    assert cleaned["forum_channel_id"] == _FORUM
    assert cleaned["notify_on_first_dm"] is True


def test_validate_blank_optional_falls_back_to_default():
    manifest = _manifest_with_optional_string()
    raw = read_extension_config_form(
        manifest,
        _form(
            [
                ("target_channel_id", _FORUM),
                ("footer_text", ""),
            ]
        ),
    )
    ok, _, cleaned = validate_extension_config(manifest, raw)
    assert ok is True
    # default from the manifest, not the empty submission
    assert cleaned["footer_text"] == "— relayed by staff"


def test_validate_rejects_bad_snowflake():
    raw = read_extension_config_form(
        _manifest(),
        _form(
            [
                ("forum_channel_id", '123"; evil()'),
                ("notify_on_first_dm", "true"),
            ]
        ),
    )
    ok, errors, cleaned = validate_extension_config(_manifest(), raw)
    assert ok is False
    assert errors
    assert cleaned == {}


def test_validate_missing_required_reports_error():
    raw = read_extension_config_form(
        _manifest(),
        _form([("notify_on_first_dm", "true")]),
    )
    ok, errors, _ = validate_extension_config(_manifest(), raw)
    assert ok is False
    assert any("forum_channel_id" in e for e in errors)


def test_validate_does_not_mutate_input():
    raw = read_extension_config_form(_manifest(), _form(_valid_form_pairs()))
    snapshot = dict(raw)
    validate_extension_config(_manifest(), raw)
    assert raw == snapshot


# --- controller: list --------------------------------------------------------


async def _seed_install(
    db_session, *, installed_version: int = 1, enabled: bool = True
) -> ExtensionInstall:
    install = ExtensionInstall(
        guild_id=_GUILD,
        extension_slug=_SLUG,
        installed_version=installed_version,
        config={
            "forum_channel_id": _FORUM,
            "notify_on_first_dm": True,
        },
        enabled=enabled,
        installed_by="admin@example.com",
    )
    db_session.add(install)
    await db_session.commit()
    await db_session.refresh(install)
    return install


async def test_list_renders_catalog(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await ExtensionsAdminController.extensions_list.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/extensions/list.html"
    assert response.context["active_page"] == "extensions"
    slugs = [ext.manifest.slug for ext in response.context["extensions"]]
    assert _SLUG in slugs
    assert response.context["installs_by_slug"] == {}


async def test_list_shows_installed_state_and_update_badge(db_session):
    await _seed_install(db_session, installed_version=0)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await ExtensionsAdminController.extensions_list.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    install = response.context["installs_by_slug"][_SLUG]
    assert install.installed_version == 0
    # manifest version (>=1) exceeds installed 0 -> update-available condition holds
    assert response.context["extensions"][0].manifest.version > install.installed_version


async def test_list_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))
    with patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=client):
        response = await ExtensionsAdminController.extensions_list.fn(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


# --- controller: install form ------------------------------------------------


async def test_install_form_renders_fields(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ExtensionsAdminController.extension_install_form.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.template_name == "admin/bot/extensions/config_form.html"
    assert response.context["mode"] == "install"
    assert response.context["install"] is None
    assert response.context["channels"]
    assert response.context["roles"]
    # every config field has a prefill value entry
    names = {f.name for f in response.context["manifest"].config}
    assert set(response.context["values"]) == names


async def test_install_form_unknown_slug_redirects(db_session):
    flash_error = Mock()
    with patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_error", flash_error):
        response = await ExtensionsAdminController.extension_install_form.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug="nope"
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


# --- controller: install POST ------------------------------------------------


async def _run_install(db_session, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success, flash_error = Mock(), Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.flash_error", flash_error
    ):
        response = await ExtensionsAdminController.extension_install.fn(
            None, request=request, db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )
    return response, flash_success, flash_error


async def test_install_post_persists_and_redirects(db_session):
    response, flash_success, _ = await _run_install(
        db_session, _form(_valid_form_pairs())
    )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/extensions"
    flash_success.assert_called_once()

    install = await get_install(db_session, _GUILD, _SLUG)
    assert install is not None
    assert install.installed_by == "admin@example.com"
    rows = (
        await db_session.execute(
            AdminHandler.__table__.select().where(
                AdminHandler.extension_install_id == install.id
            )
        )
    ).fetchall()
    assert len(rows) == len(_manifest().handlers)


async def test_install_post_invalid_rerenders_400(db_session):
    response, _, _ = await _run_install(
        db_session,
        _form([("forum_channel_id", "not-a-snowflake"), ("staff_role_id", _STAFF)]),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/extensions/config_form.html"
    assert response.context["errors"]
    # entered values are preserved for the re-render
    assert response.context["values"]["forum_channel_id"] == "not-a-snowflake"
    assert await get_install(db_session, _GUILD, _SLUG) is None


async def test_install_post_duplicate_flashes_and_400(db_session):
    await _run_install(db_session, _form(_valid_form_pairs()))
    response, _, flash_error = await _run_install(
        db_session, _form(_valid_form_pairs())
    )

    assert response.status_code == 400
    flash_error.assert_called_once()
    installs = await list_installs(db_session, _GUILD)
    assert len(installs) == 1


# --- controller: configure ---------------------------------------------------


async def test_configure_form_prefills_from_install(db_session):
    await _seed_install(db_session)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ExtensionsAdminController.extension_configure_form.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.context["mode"] == "configure"
    assert response.context["values"]["forum_channel_id"] == _FORUM


async def test_configure_form_not_installed_redirects(db_session):
    flash_error = Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_error", flash_error):
        response = await ExtensionsAdminController.extension_configure_form.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


async def test_configure_post_applies_new_config(db_session):
    await _run_install(db_session, _form(_valid_form_pairs()))
    new_forum = "345678901234567890"
    request = SimpleNamespace(
        form=AsyncMock(
            return_value=_form(
                [
                    ("forum_channel_id", new_forum),
                    ("notify_on_first_dm", "true"),
                ]
            )
        )
    )
    flash_success = Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value=_ctx())
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success):
        response = await ExtensionsAdminController.extension_configure.fn(
            None, request=request, db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    install = await get_install(db_session, _GUILD, _SLUG)
    assert install.config["forum_channel_id"] == new_forum


# --- controller: enable / disable / uninstall --------------------------------


async def test_disable_then_enable(db_session):
    await _run_install(db_session, _form(_valid_form_pairs()))

    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await ExtensionsAdminController.extension_disable.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )
    assert response.status_code in (302, 303, 307)
    install = await get_install(db_session, _GUILD, _SLUG)
    assert install.enabled is False

    with patch(f"{_MODULE}.flash_success", flash_success):
        await ExtensionsAdminController.extension_enable.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )
    await db_session.refresh(install)
    assert install.enabled is True


async def test_enable_not_installed_flashes_error(db_session):
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await ExtensionsAdminController.extension_enable.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


async def test_uninstall_removes_install_and_rows(db_session):
    await _run_install(db_session, _form(_valid_form_pairs()))
    install = await get_install(db_session, _GUILD, _SLUG)
    install_id = install.id

    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await ExtensionsAdminController.extension_uninstall.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    assert await get_install(db_session, _GUILD, _SLUG) is None
    rows = (
        await db_session.execute(
            AdminHandler.__table__.select().where(
                AdminHandler.extension_install_id == install_id
            )
        )
    ).fetchall()
    assert rows == []


# --- controller: update ------------------------------------------------------


async def test_update_reapplies_and_redirects(db_session):
    await _run_install(db_session, _form(_valid_form_pairs()))
    # simulate an older installed version so update advances it
    install = await get_install(db_session, _GUILD, _SLUG)
    install.installed_version = 0
    await db_session.commit()

    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await ExtensionsAdminController.extension_update.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug=_SLUG
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    await db_session.refresh(install)
    assert install.installed_version == _manifest().version


async def test_update_unknown_slug_redirects(db_session):
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await ExtensionsAdminController.extension_update.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, slug="nope"
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        ExtensionsAdminController.extensions_list,
        ExtensionsAdminController.extension_install_form,
        ExtensionsAdminController.extension_install,
        ExtensionsAdminController.extension_configure_form,
        ExtensionsAdminController.extension_configure,
        ExtensionsAdminController.extension_update,
        ExtensionsAdminController.extension_enable,
        ExtensionsAdminController.extension_disable,
        ExtensionsAdminController.extension_uninstall,
    ],
)
def test_routes_require_admin(handler):
    guards = handler.guards
    assert auth_guard in guards
    admin_guards = [
        g
        for g in guards
        if isinstance(g, Permission) and g.permission == "administrator"
    ]
    assert admin_guards, "route must require the administrator permission"


async def test_administrator_permission_denies_non_admin_and_allows_admin():
    guard = Permission("administrator")
    non_admin = SimpleNamespace(permissions={"view-drafts"}, roles=set())
    admin = SimpleNamespace(permissions={"administrator"}, roles=set())

    assert await guard.check(non_admin) is False
    assert await guard.check(admin) is True
