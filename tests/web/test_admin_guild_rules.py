"""Tests for the guild-rules page on the Skrift admin per-guild config controller.

Follows ``tests/web/test_admin_guild_configs.py``: the pure form parser, the
loader, both handlers, the template and the auth wiring.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard

from smarter_dev.web.bot_admin.guild_configs import GuildConfigsAdminController
from smarter_dev.web.bot_admin.guild_configs import load_or_create_guild_rules_config
from smarter_dev.web.bot_admin.guild_configs import parse_guild_rules_form
from smarter_dev.web.crud import GuildRulesConfigOperations
from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.discord_admin_client import GuildNotFoundError
from smarter_dev.web.guild_rules import parse_guild_rules
from smarter_dev.web.models import GuildRulesConfig

_GUILD = "111111111111111111"
_MODULE = "smarter_dev.web.bot_admin.guild_configs"
_SQUADS_MODULE = "smarter_dev.web.bot_admin.squads"
_TEMPLATES_DIR = Path(__file__).parents[2] / "templates"

_RULES = "## No self-promotion\nKeep links in #showcase.\n\n## Be kind\nAlways.\n"


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
        get_guild_channels=AsyncMock(return_value=[]),
        get_guild_roles=AsyncMock(return_value=[]),
    )


@contextmanager
def _patch_get(client: SimpleNamespace):
    """Patch every collaborator a GET handler reaches, across both modules."""
    with patch(
        f"{_SQUADS_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_SQUADS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        yield


# --- parse_guild_rules_form (pure) -------------------------------------------


def test_parse_guild_rules_form_keeps_the_markdown():
    parsed = parse_guild_rules_form({"rules_markdown": _RULES})

    assert parsed == {"rules_markdown": _RULES.strip()}


def test_parse_guild_rules_form_normalises_browser_crlf_endings():
    parsed = parse_guild_rules_form({"rules_markdown": _RULES.replace("\n", "\r\n")})

    assert "\r" not in parsed["rules_markdown"]
    assert parse_guild_rules(parsed["rules_markdown"]) == parse_guild_rules(_RULES)


def test_parse_guild_rules_form_missing_field_is_empty():
    assert parse_guild_rules_form({}) == {"rules_markdown": ""}


def test_parse_guild_rules_form_whitespace_only_is_empty():
    assert parse_guild_rules_form({"rules_markdown": "  \n\n\t "}) == {
        "rules_markdown": ""
    }


def test_parse_guild_rules_form_ignores_unknown_keys():
    parsed = parse_guild_rules_form({"rules_markdown": "## A\nb", "surprise": "boom"})

    assert set(parsed) == {"rules_markdown"}


def test_parse_guild_rules_form_never_raises_on_malformed_markdown():
    parsed = parse_guild_rules_form({"rules_markdown": "#" * 500})

    assert isinstance(parsed["rules_markdown"], str)


# --- loader -------------------------------------------------------------------


async def test_load_or_create_guild_rules_config_persists_default(db_session):
    config = await load_or_create_guild_rules_config(db_session, _GUILD)

    assert config.guild_id == _GUILD
    assert config.rules_markdown == ""
    persisted = await GuildRulesConfigOperations().get_config(db_session, _GUILD)
    assert persisted is not None


# --- controller GET -----------------------------------------------------------


def _rules_get():
    return GuildConfigsAdminController.guild_rules_config.fn


async def test_rules_get_renders_form_with_the_parse_preview(db_session):
    db_session.add(GuildRulesConfig(guild_id=_GUILD, rules_markdown=_RULES))
    await db_session.commit()

    with _patch_get(_admin_client()):
        response = await _rules_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/guild_configs/guild_rules.html"
    assert response.context["active_page"] == "guild_rules"
    assert response.context["config"].rules_markdown == _RULES
    parsed = response.context["parsed_rules"]
    assert [rule.index for rule in parsed] == [1, 2]
    assert [rule.title for rule in parsed] == ["No self-promotion", "Be kind"]


async def test_rules_get_on_a_fresh_guild_previews_no_rules(db_session):
    with _patch_get(_admin_client()):
        response = await _rules_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.context["parsed_rules"] == []


async def test_rules_get_previews_malformed_markdown_without_raising(db_session):
    db_session.add(
        GuildRulesConfig(guild_id=_GUILD, rules_markdown="no headings at all")
    )
    await db_session.commit()

    with _patch_get(_admin_client()):
        response = await _rules_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.context["parsed_rules"] == []
    assert response.context["config"].rules_markdown == "no headings at all"


async def test_rules_get_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=GuildNotFoundError("nope"))
    )
    with _patch_get(client):
        response = await _rules_get()(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404


async def test_rules_get_discord_error_returns_503(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=DiscordAdminError("upstream boom"))
    )
    with _patch_get(client):
        response = await _rules_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code == 503


# --- controller POST ----------------------------------------------------------


def _rules_post():
    return GuildConfigsAdminController.save_guild_rules_config.fn


async def test_rules_post_persists_and_redirects(db_session):
    request = SimpleNamespace(form=AsyncMock(return_value={"rules_markdown": _RULES}))
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await _rules_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/rules"

    saved = await GuildRulesConfigOperations().get_config(db_session, _GUILD)
    assert saved.rules_markdown == _RULES.strip()
    flash_success.assert_called_once()
    assert "2" in flash_success.call_args.args[1]


async def test_rules_post_stores_lf_endings_for_a_crlf_submission(db_session):
    request = SimpleNamespace(
        form=AsyncMock(return_value={"rules_markdown": _RULES.replace("\n", "\r\n")})
    )
    with patch(f"{_MODULE}.flash_success", Mock()):
        await _rules_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    saved = await GuildRulesConfigOperations().get_config(db_session, _GUILD)
    assert "\r" not in saved.rules_markdown
    assert len(parse_guild_rules(saved.rules_markdown)) == 2


async def test_rules_post_warns_when_nothing_parsed_but_still_saves(db_session):
    request = SimpleNamespace(
        form=AsyncMock(return_value={"rules_markdown": "1) no spam\n2) be kind"})
    )
    flash_warning = Mock()
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_warning", flash_warning), patch(
        f"{_MODULE}.flash_success", flash_success
    ):
        await _rules_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    flash_warning.assert_called_once()
    flash_success.assert_not_called()
    saved = await GuildRulesConfigOperations().get_config(db_session, _GUILD)
    assert saved.rules_markdown == "1) no spam\n2) be kind"


async def test_rules_post_clearing_the_textarea_saves_empty_without_warning(
    db_session,
):
    db_session.add(GuildRulesConfig(guild_id=_GUILD, rules_markdown=_RULES))
    await db_session.commit()

    request = SimpleNamespace(form=AsyncMock(return_value={"rules_markdown": "   "}))
    flash_warning = Mock()
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_warning", flash_warning), patch(
        f"{_MODULE}.flash_success", flash_success
    ):
        await _rules_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    flash_warning.assert_not_called()
    flash_success.assert_called_once()
    saved = await GuildRulesConfigOperations().get_config(db_session, _GUILD)
    assert saved.rules_markdown == ""


# --- template -----------------------------------------------------------------


def _render_rules_template(config, parsed_rules) -> str:
    stub_base = (
        "{% block title %}{% endblock %}{% block admin_content %}{% endblock %}"
    )
    environment = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"admin/base.html": stub_base}),
                FileSystemLoader(_TEMPLATES_DIR),
            ]
        ),
        autoescape=True,
    )
    environment.globals["site_name"] = lambda: "Smarter Dev"
    return environment.get_template(
        "admin/bot/guild_configs/guild_rules.html"
    ).render(
        guild=_guild_detail(),
        config=config,
        parsed_rules=parsed_rules,
        active_page="guild_rules",
        guild_id=_GUILD,
        flash_messages=[],
    )


def test_rules_template_renders_the_textarea_and_the_preview():
    config = GuildRulesConfig(guild_id=_GUILD, rules_markdown=_RULES)

    html = _render_rules_template(config, parse_guild_rules(_RULES))

    assert f'action="/admin/bot/guilds/{_GUILD}/rules"' in html
    assert 'name="rules_markdown"' in html
    assert "Keep links in #showcase." in html
    # The preview names the count and every index/title pair.
    assert "2 rules" in html
    assert "No self-promotion" in html
    assert "Be kind" in html


def test_rules_template_documents_the_heading_format():
    html = _render_rules_template(GuildRulesConfig.get_defaults(_GUILD), [])

    assert "##" in html
    assert "heading" in html.lower()


def test_rules_template_warns_when_nothing_parsed():
    config = GuildRulesConfig(guild_id=_GUILD, rules_markdown="no headings at all")

    html = _render_rules_template(config, [])

    assert "0 rules" in html


def test_rules_template_survives_an_empty_document():
    html = _render_rules_template(GuildRulesConfig.get_defaults(_GUILD), [])

    assert 'name="rules_markdown"' in html


def test_sidebar_links_the_guild_rules_page():
    sidebar = (_TEMPLATES_DIR / "admin" / "bot" / "_sidebar.html").read_text()

    assert "/rules" in sidebar
    assert "active_page == 'guild_rules'" in sidebar


# --- auth wiring --------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        GuildConfigsAdminController.guild_rules_config,
        GuildConfigsAdminController.save_guild_rules_config,
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
