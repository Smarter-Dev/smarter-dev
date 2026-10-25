"""scripts/delete_retired_moderation_rows.py (#240): the disabled scam-banner
and edit-ping-catch rows go, in one transaction, and only once the
coordinator owns the guild; every refusal changes nothing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ExtensionInstall
from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.moderation_gateway.gateway import default_session_factory

GUILD = "644299523686006834"
OTHER_GUILD = "1"


def sessions():
    return default_session_factory()()


@pytest.fixture
def script(monkeypatch):
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "delete_retired_moderation_rows.py"
    )
    spec = importlib.util.spec_from_file_location("delete_retired_rows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_db_session_context", default_session_factory())
    return module


def _handler(guild_id: str, name: str, *, install=None, enabled=False, channels=()):
    return AdminHandler(
        guild_id=guild_id,
        name=name,
        trigger_type="message",
        settings={},
        channel_ids=list(channels),
        description="d",
        script="pass\n",
        created_by_admin="extension" if install else "A",
        enabled=enabled,
        extension_install_id=install.id if install else None,
        extension_handler_key=name if install else None,
    )


def _install(guild_id: str, slug: str, *, enabled=False) -> ExtensionInstall:
    return ExtensionInstall(
        guild_id=guild_id,
        extension_slug=slug,
        installed_version=1,
        config={},
        enabled=enabled,
        installed_by="admin",
    )


async def _after_cutover(guild_id: str = GUILD, *, owner: str | None = "coordinator"):
    """A guild as the cutover left it: retired rows disabled, Disboard live."""
    async with sessions() as session:
        if owner is not None:
            session.add(
                ModerationOwnership(
                    guild_id=guild_id,
                    owner=owner,
                    generation=1,
                    rules=[],
                    legacy_state={"handlers": {}},
                )
            )
        edit_install = _install(guild_id, "edit-ping-catch")
        disboard_install = _install(guild_id, "disboard-bumping", enabled=True)
        session.add_all([edit_install, disboard_install])
        await session.flush()
        session.add_all(
            [
                _handler(guild_id, "scam-banner"),
                _handler(guild_id, "edit-ping-catch", install=edit_install),
                _handler(
                    guild_id,
                    "disboard-bump-tracker",
                    install=disboard_install,
                    enabled=True,
                    channels=["702178352395583498"],
                ),
                _handler(guild_id, "welcome", enabled=True),
            ]
        )
        await session.commit()


async def _rows(guild_id: str = GUILD) -> tuple[set[str], set[str]]:
    async with sessions() as session:
        handlers = set(
            (
                await session.execute(
                    select(AdminHandler.name).where(AdminHandler.guild_id == guild_id)
                )
            ).scalars()
        )
        installs = set(
            (
                await session.execute(
                    select(ExtensionInstall.extension_slug).where(
                        ExtensionInstall.guild_id == guild_id
                    )
                )
            ).scalars()
        )
    return handlers, installs


ALL_HANDLERS = {"scam-banner", "edit-ping-catch", "disboard-bump-tracker", "welcome"}
ALL_INSTALLS = {"edit-ping-catch", "disboard-bumping"}


@pytest.mark.parametrize("owner", ["legacy", None], ids=["legacy", "no_row"])
async def test_refused_unless_the_coordinator_owns_the_guild(script, owner, capsys):
    await _after_cutover(owner=owner)
    assert await script.main(["check", "--guild", GUILD]) == 2
    assert await script.main(["delete", "--guild", GUILD]) == 2
    assert await _rows() == (ALL_HANDLERS, ALL_INSTALLS)
    assert "deleted" not in capsys.readouterr().out


@pytest.mark.parametrize("what", ["scam-banner", "edit-ping-catch", "install"])
async def test_refused_while_a_retired_row_is_enabled(script, what):
    await _after_cutover()
    async with sessions() as session:
        if what == "install":
            row = (
                await session.execute(
                    select(ExtensionInstall).where(
                        ExtensionInstall.extension_slug == "edit-ping-catch"
                    )
                )
            ).scalar_one()
        else:
            row = (
                await session.execute(
                    select(AdminHandler).where(AdminHandler.name == what)
                )
            ).scalar_one()
        row.enabled = True
        await session.commit()
    assert await script.main(["delete", "--guild", GUILD]) == 2
    assert await _rows() == (ALL_HANDLERS, ALL_INSTALLS)


async def test_refused_when_the_edit_ping_handler_belongs_to_another_install(script):
    await _after_cutover()
    async with sessions() as session:
        disboard = (
            await session.execute(
                select(ExtensionInstall).where(
                    ExtensionInstall.extension_slug == "disboard-bumping"
                )
            )
        ).scalar_one()
        row = (
            await session.execute(
                select(AdminHandler).where(AdminHandler.name == "edit-ping-catch")
            )
        ).scalar_one()
        row.extension_install_id = disboard.id
        await session.commit()
    assert await script.main(["delete", "--guild", GUILD]) == 2
    assert await _rows() == (ALL_HANDLERS, ALL_INSTALLS)


async def test_refused_when_a_retired_handler_is_channel_scoped(script):
    """The coordinator reads the scam gate's scope from the row: deleting a
    scoped row would widen the rule to every channel."""
    await _after_cutover()
    async with sessions() as session:
        row = (
            await session.execute(
                select(AdminHandler).where(AdminHandler.name == "scam-banner")
            )
        ).scalar_one()
        row.channel_ids = ["123"]
        await session.commit()
    assert await script.main(["delete", "--guild", GUILD]) == 2
    assert await _rows() == (ALL_HANDLERS, ALL_INSTALLS)


async def test_check_lists_the_rows_and_changes_nothing(script, capsys):
    await _after_cutover()
    assert await script.main(["check", "--guild", GUILD]) == 0
    out = capsys.readouterr().out
    assert "would delete admin_handlers" in out
    assert "name=scam-banner enabled=False" in out
    assert "name=edit-ping-catch enabled=False" in out
    assert "extension_installs" in out
    assert "disboard" not in out and "welcome" not in out
    assert await _rows() == (ALL_HANDLERS, ALL_INSTALLS)


async def test_delete_removes_exactly_the_three_rows_and_is_idempotent(script, capsys):
    await _after_cutover()
    await _after_cutover(OTHER_GUILD)

    assert await script.main(["delete", "--guild", GUILD]) == 0
    out = capsys.readouterr().out
    assert out.count("deleted ") == 3
    assert await _rows() == (
        {"disboard-bump-tracker", "welcome"},
        {"disboard-bumping"},
    )
    # The other guild's rows are its own.
    assert await _rows(OTHER_GUILD) == (ALL_HANDLERS, ALL_INSTALLS)

    assert await script.main(["delete", "--guild", GUILD]) == 0
    assert "nothing to delete" in capsys.readouterr().out
    assert await script.main(["check", "--guild", GUILD]) == 0
    assert "nothing to delete" in capsys.readouterr().out


async def test_delete_without_the_install_removes_the_handlers(script):
    """An install already uninstalled by hand: the handlers still go."""
    await _after_cutover()
    async with sessions() as session:
        install = (
            await session.execute(
                select(ExtensionInstall).where(
                    ExtensionInstall.extension_slug == "edit-ping-catch"
                )
            )
        ).scalar_one()
        await session.delete(install)
        await session.commit()
    assert await script.main(["delete", "--guild", GUILD]) == 0
    assert await _rows() == (
        {"disboard-bump-tracker", "welcome"},
        {"disboard-bumping"},
    )
