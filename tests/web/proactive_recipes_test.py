"""Recipes for the proactive agent (#267): preview rule, store, bot API, admin.

The preview is the one rule every surface shares: the bot API hands it to the
agent, the admin list and form render it, and the edit form's script redraws
it as the admin types. The script's copy is run under node against the
server's on the same texts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from litestar.testing import create_async_test_client

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.web.api_native import proactive_recipes as api_module
from smarter_dev.web.api_native.proactive_recipes import ProactiveRecipesController
from smarter_dev.web.bot_admin.proactive_recipes import ProactiveRecipesAdminController
from smarter_dev.web.chat_bot_purge import hit_counts
from smarter_dev.web.chat_bot_purge import receipt_summary
from smarter_dev.web.chat_bot_purge import scan_stores
from smarter_dev.web.crud import ConflictError
from smarter_dev.web.crud import create_proactive_recipe
from smarter_dev.web.crud import get_proactive_recipe
from smarter_dev.web.crud import get_proactive_recipe_preview_chars
from smarter_dev.web.crud import list_proactive_recipes
from smarter_dev.web.crud import set_proactive_recipe_preview_chars
from smarter_dev.web.crud import update_proactive_recipe
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.discord_admin_client import GuildNotFoundError
from smarter_dev.web.models import PROACTIVE_RECIPE_PREVIEW_DEFAULT
from smarter_dev.web.proactive_recipes import RecipeForm
from smarter_dev.web.proactive_recipes import parse_preview_chars
from smarter_dev.web.proactive_recipes import read_recipe_form
from smarter_dev.web.proactive_recipes import recipe_form_errors
from smarter_dev.web.proactive_recipes import recipe_name_key
from smarter_dev.web.proactive_recipes import recipe_preview
from tests.web.admin_template_rendering import admin_template_source
from tests.web.admin_template_rendering import render_admin_template

GUILD = "111111111111111111"
OTHER_GUILD = "222222222222222222"
KAI_ID = "333333333333333333"
_MODULE = "smarter_dev.web.bot_admin.proactive_recipes"
_CAMPAIGNS_MODULE = "smarter_dev.web.bot_admin.campaigns"
_FORM_TEMPLATE = "admin/bot/proactive_recipes/form.html"

WELCOME = (
    "Use this when someone new says hello in #welcome.\n\n"
    "1. Greet them by name.\n2. Point them at #rules."
)


async def add_recipe(
    session, name="Welcome", instructions=WELCOME, *, enabled=True, guild_id=GUILD
):
    recipe = await create_proactive_recipe(
        session,
        guild_id=guild_id,
        name=name,
        name_key=recipe_name_key(name),
        instructions=instructions,
        enabled=enabled,
    )
    await session.commit()
    return recipe


# -- The preview rule -----------------------------------------------------------


def test_the_preview_is_the_first_paragraph():
    assert recipe_preview(WELCOME, 300) == (
        "Use this when someone new says hello in #welcome."
    )


def test_the_preview_joins_the_first_paragraphs_lines():
    text = "Use this  when\nsomeone asks\tabout\r\nlinks.\n \nNot this."
    assert recipe_preview(text, 300) == "Use this when someone asks about links."


def test_a_long_paragraph_is_cut_to_the_limit_with_the_ellipsis_counted():
    preview = recipe_preview("word " * 200, 50)
    assert preview.endswith("…")
    assert len(preview) <= 50
    assert not preview[:-1].endswith(" ")


def test_a_paragraph_at_the_limit_is_not_cut():
    assert recipe_preview("x" * 300, 300) == "x" * 300
    assert recipe_preview("x" * 301, 300) == "x" * 299 + "…"


def test_leading_blank_lines_are_skipped():
    assert recipe_preview("\n\n  \nFirst.\n\nSecond.", 300) == "First."


# The edit form's script must cut where the server does (Zech, #267).
AGREEMENT_SAMPLES = [
    ("blank line inside the limit", WELCOME, 300),
    ("no blank line", "Use this for releases.\nPost the notes. Ping the role.", 300),
    ("long first line", "Explain the rule carefully. " * 40, 300),
    ("long first line, small limit", "Explain the rule carefully. " * 40, 50),
    ("leading blank lines", "\n\n \t\nUse this first.\n\nThen this.", 300),
    ("CRLF", "Use this on Fridays.\r\nShare the\r\nlinks.\r\n\r\nSecond.", 300),
    ("CRLF blank line with spaces", "One.\r\n  \r\nTwo.", 300),
    ("emoji counted as one each", "🎉" * 120 + " party", 100),
    ("non-breaking space kept", "Keep this together. " * 30, 60),
    ("cut lands on a space", "a" * 48 + " " + "b" * 40, 50),
    ("exactly the limit", "y" * 50, 50),
    ("one over the limit", "y" * 51, 50),
    ("form feed and vertical tab", "One\x0btwo\x0cthree.\n\nFour.", 300),
    ("only whitespace", " \n\t\n ", 300),
    ("lone CR", "One\rtwo.", 300),
]


def _form_script() -> str:
    source = admin_template_source(_FORM_TEMPLATE)
    return source.split("// recipe-preview:start", 1)[1].split(
        "// recipe-preview:end", 1
    )[0]


@pytest.mark.skipif(
    shutil.which("node") is None and not os.environ.get("CI"),
    reason="node is not installed (required, and never skipped, under CI)",
)
def test_the_edit_forms_script_cuts_where_the_server_does(tmp_path):
    # Under CI a missing node fails instead of skipping: a skipped check would
    # pass a script that no longer cuts where the server does.
    if shutil.which("node") is None:
        pytest.fail("node is required to check the edit form's preview script")
    samples = [{"text": text, "limit": limit} for _, text, limit in AGREEMENT_SAMPLES]
    script = tmp_path / "recipe_preview.js"
    script.write_text(
        _form_script()
        + "\nconst samples = "
        + json.dumps(samples)
        + ";\nprocess.stdout.write(JSON.stringify(samples.map(s => {"
        "const r = recipePreview(s.text, s.limit);"
        "return {preview: r.preview, length: r.length, reason: r.reason,"
        " marked: r.chars.slice(r.start, r.end).join('')}; })));\n"
    )
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout)
    for (label, text, limit), output in zip(AGREEMENT_SAMPLES, outputs, strict=True):
        expected = recipe_preview(text, limit)
        assert output["preview"] == expected, label
        assert output["length"] == len(expected), label
        # The highlighted source, collapsed, is the preview without its ellipsis.
        marked = re.sub(r"[ \t\n\r\f\v]+", " ", output["marked"])
        assert marked == expected.removesuffix("…").rstrip(" "), label
        assert output["reason"] == _reason(text, limit), label


def _reason(text: str, limit: int) -> str:
    """Why the server's preview ends where it does, worked out separately."""
    body = text.replace("\r\n", "\n").strip(" \t\n\r\f\v")
    parts = re.split(r"\n[ \t]*\n", body, maxsplit=1)
    if len(" ".join(re.split(r"[ \t\n\r\f\v]+", parts[0])).strip(" ")) > limit:
        return "limit"
    return "blank_line" if len(parts) > 1 else "whole"


def test_the_edit_form_redraws_the_preview_from_the_live_setting():
    html = render_admin_template(
        _FORM_TEMPLATE,
        guild=SimpleNamespace(id=GUILD, name="Alpha"),
        recipe=None,
        form_data=RecipeForm(name="Welcome", instructions=WELCOME, enabled=True),
        preview=recipe_preview(WELCOME, 120),
        preview_chars=120,
        name_max=80,
        instructions_max=20000,
        errors=[],
        flash_messages=[],
        guild_id=GUILD,
        active_page="proactive_recipes",
    )
    assert 'data-preview-chars="120"' in html
    assert '<script nonce="test-nonce">' in html
    assert 'box.addEventListener("input", render)' in html
    assert "Use this when someone new says hello in #welcome." in html


# -- Form and setting parsing ----------------------------------------------------


def test_names_match_ignoring_case_and_spacing():
    assert recipe_name_key("  Weekly   Challenge ") == recipe_name_key(
        "weekly challenge"
    )


def test_the_form_is_trimmed_and_checked():
    data = read_recipe_form(
        {"name": "  Weekly   post ", "instructions": "  Do it.\r\n", "enabled": "on"}
    )
    assert data == RecipeForm(name="Weekly post", instructions="Do it.", enabled=True)
    assert recipe_form_errors(data) == []
    assert recipe_form_errors(RecipeForm("", "", False)) == [
        "Give the recipe a name.",
        "Write the recipe's instructions.",
    ]
    assert recipe_form_errors(RecipeForm("x" * 81, "y" * 20_001, True))[0].startswith(
        "The name is 81"
    )


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("300", 300),
        (" 50 ", 50),
        ("2000", 2000),
        ("49", None),
        ("2001", None),
        ("x", None),
        (None, None),
    ],
)
def test_preview_length_bounds(raw, value):
    assert parse_preview_chars(raw) == value


# -- Store ---------------------------------------------------------------------


async def test_a_name_is_taken_once_per_guild(db_session):
    await add_recipe(db_session, "Welcome")
    with pytest.raises(ConflictError):
        await add_recipe(db_session, "  WELCOME ")
    await db_session.rollback()
    await add_recipe(db_session, "Welcome", guild_id=OTHER_GUILD)


async def test_only_a_change_to_the_name_or_text_bumps_the_revision(db_session):
    recipe = await add_recipe(db_session)
    assert recipe.revision == 1

    async def save(**over):
        fields = {
            "name": recipe.name,
            "name_key": recipe.name_key,
            "instructions": recipe.instructions,
            "enabled": recipe.enabled,
        } | over
        changed = await update_proactive_recipe(db_session, recipe, **fields)
        await db_session.commit()
        return changed

    assert await save(enabled=False) is False
    assert recipe.revision == 1
    assert await save(instructions=WELCOME + "\n3. Wave.") is True
    assert recipe.revision == 2
    assert await save(name="Hello", name_key="hello") is True
    assert recipe.revision == 3


async def test_the_preview_length_defaults_to_300(db_session):
    assert await get_proactive_recipe_preview_chars(db_session, GUILD) == 300
    assert PROACTIVE_RECIPE_PREVIEW_DEFAULT == 300
    await set_proactive_recipe_preview_chars(db_session, GUILD, 120)
    await set_proactive_recipe_preview_chars(db_session, GUILD, 80)
    await db_session.commit()
    assert await get_proactive_recipe_preview_chars(db_session, GUILD) == 80
    assert await get_proactive_recipe_preview_chars(db_session, OTHER_GUILD) == 300


# -- Bot API ---------------------------------------------------------------------


@pytest.fixture
async def client(db_session) -> AsyncIterator[AsyncTestClient]:
    original_guards = list(api_module.BOT_API_GUARDS)
    api_module.BOT_API_GUARDS.clear()
    try:
        async with create_async_test_client(
            route_handlers=[ProactiveRecipesController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            yield test_client
    finally:
        api_module.BOT_API_GUARDS[:] = original_guards


async def test_the_list_holds_enabled_recipes_as_name_and_preview(client, db_session):
    welcome = await add_recipe(db_session)
    long_text = "Use this for the weekly challenge post, every Monday morning. " * 3
    await add_recipe(db_session, "Challenge", long_text)
    await add_recipe(db_session, "Secret", "Hidden.", enabled=False)
    await add_recipe(db_session, "Elsewhere", "Other guild.", guild_id=OTHER_GUILD)
    await set_proactive_recipe_preview_chars(db_session, GUILD, 50)
    await db_session.commit()

    response = await client.get(f"/api/guilds/{GUILD}/proactive-recipes")

    assert response.status_code == 200
    body = response.json()
    assert body["preview_chars"] == 50
    challenge, listed_welcome = body["recipes"]
    assert [challenge["name"], listed_welcome["name"]] == ["Challenge", "Welcome"]
    assert challenge["preview"] == recipe_preview(long_text, 50)
    assert challenge["preview"].endswith("…") and len(challenge["preview"]) <= 50
    assert listed_welcome["preview"] == recipe_preview(WELCOME, 50)
    assert listed_welcome["id"] == str(welcome.id)
    assert listed_welcome["revision"] == 1
    assert "instructions" not in listed_welcome


async def test_a_guild_with_no_recipes_lists_none(client):
    response = await client.get(f"/api/guilds/{GUILD}/proactive-recipes")
    assert response.status_code == 200
    assert response.json() == {"preview_chars": 300, "recipes": []}


async def test_opening_a_recipe_returns_it_whole(client, db_session):
    recipe = await add_recipe(db_session)
    response = await client.get(
        f"/api/guilds/{GUILD}/proactive-recipes/by-name", params={"name": " welcome "}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["instructions"] == WELCOME
    assert body["name"] == "Welcome"
    assert body["id"] == str(recipe.id)
    assert body["revision"] == 1


@pytest.mark.parametrize("name", ["Secret", "Nope", "Elsewhere"])
async def test_a_switched_off_unknown_or_other_guilds_recipe_is_not_found(
    client, db_session, name
):
    await add_recipe(db_session, "Secret", "Hidden.", enabled=False)
    await add_recipe(db_session, "Elsewhere", "Other guild.", guild_id=OTHER_GUILD)
    response = await client.get(
        f"/api/guilds/{GUILD}/proactive-recipes/by-name", params={"name": name}
    )
    assert response.status_code == 404


async def test_the_routes_refuse_a_caller_without_a_key():
    async with create_async_test_client(
        route_handlers=[ProactiveRecipesController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: None, sync_to_thread=False)},
    ) as guarded:
        listed = await guarded.get(f"/api/guilds/{GUILD}/proactive-recipes")
        opened = await guarded.get(
            f"/api/guilds/{GUILD}/proactive-recipes/by-name", params={"name": "x"}
        )
    assert listed.status_code in (401, 403)
    assert opened.status_code in (401, 403)


# -- Admin pages -------------------------------------------------------------------


class _Request:
    def __init__(self, form=None):
        self._form = form or {}

    async def form(self):
        return self._form


def _admin_patches(csrf=True):
    client = SimpleNamespace(
        get_guild=AsyncMock(
            return_value=DiscordGuildDetail(
                id=GUILD,
                name="Alpha Guild",
                icon=None,
                owner_id="owner",
                member_count=3,
                description=None,
            )
        )
    )
    return [
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=client),
        patch(f"{_MODULE}.get_flash_messages", return_value=[]),
        patch(f"{_MODULE}.flash_success"),
        patch(f"{_MODULE}.flash_error"),
        patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=csrf)),
    ]


async def _call(handler, db_session, *, csrf=True, form=None, **kwargs):
    patches = _admin_patches(csrf)
    for item in patches:
        item.start()
    try:
        return await handler.fn(
            None,
            request=_Request(form),
            db_session=db_session,
            guild_id=GUILD,
            **kwargs,
        )
    finally:
        for item in reversed(patches):
            item.stop()


async def test_an_admin_creates_edits_switches_and_deletes_a_recipe(db_session):
    controller = ProactiveRecipesAdminController
    form = {
        "name": "Welcome",
        "instructions": WELCOME,
        "enabled": "on",
        "action": "save",
    }
    response = await _call(controller.recipe_create, db_session, form=form)
    assert response.url == f"/admin/bot/guilds/{GUILD}/proactive-recipes"
    (recipe,) = await list_proactive_recipes(db_session, GUILD)
    assert recipe.revision == 1 and recipe.enabled

    edited = {**form, "instructions": WELCOME + "\n3. Wave."}
    await _call(controller.recipe_edit, db_session, form=edited, recipe_id=recipe.id)
    recipe = await get_proactive_recipe(db_session, GUILD, recipe.id)
    assert recipe.revision == 2
    assert recipe.instructions.endswith("3. Wave.")

    await _call(controller.recipe_toggle, db_session, recipe_id=recipe.id)
    recipe = await get_proactive_recipe(db_session, GUILD, recipe.id)
    assert recipe.enabled is False
    assert recipe.revision == 2

    await _call(controller.recipe_delete, db_session, recipe_id=recipe.id)
    assert await list_proactive_recipes(db_session, GUILD) == []


async def test_the_list_shows_the_agents_preview_at_the_guilds_length(db_session):
    await add_recipe(db_session)
    await _call(
        ProactiveRecipesAdminController.recipes_settings,
        db_session,
        form={"preview_chars": "50"},
    )
    response = await _call(ProactiveRecipesAdminController.recipes_list, db_session)

    assert response.template_name == "admin/bot/proactive_recipes/list.html"
    ((recipe, preview),) = response.context["recipes"]
    assert response.context["preview_chars"] == 50
    assert preview == recipe_preview(WELCOME, 50)
    html = render_admin_template(response.template_name, **response.context)
    assert preview in html
    assert 'name="preview_chars" value="50"' in html


async def test_show_preview_renders_without_saving(db_session):
    response = await _call(
        ProactiveRecipesAdminController.recipe_create,
        db_session,
        form={"name": "Welcome", "instructions": WELCOME, "action": "preview"},
    )
    assert response.template_name == _FORM_TEMPLATE
    assert response.context["preview"] == recipe_preview(WELCOME, 300)
    assert await list_proactive_recipes(db_session, GUILD) == []


async def test_a_duplicate_name_is_refused_on_the_form(db_session):
    await add_recipe(db_session)
    response = await _call(
        ProactiveRecipesAdminController.recipe_create,
        db_session,
        form={"name": "welcome", "instructions": "Other.", "action": "save"},
    )
    assert response.status_code == 400
    assert response.context["errors"] == ["A recipe named “welcome” already exists."]


async def test_a_post_without_a_valid_token_changes_nothing(db_session):
    recipe = await add_recipe(db_session)
    for handler in (
        ProactiveRecipesAdminController.recipe_toggle,
        ProactiveRecipesAdminController.recipe_delete,
    ):
        await _call(handler, db_session, csrf=False, recipe_id=recipe.id)
    await _call(
        ProactiveRecipesAdminController.recipe_edit,
        db_session,
        csrf=False,
        form={"name": "Changed", "instructions": "Changed.", "action": "save"},
        recipe_id=recipe.id,
    )
    await _call(
        ProactiveRecipesAdminController.recipes_settings,
        db_session,
        csrf=False,
        form={"preview_chars": "60"},
    )
    stored = await get_proactive_recipe(db_session, GUILD, recipe.id)
    assert (stored.name, stored.enabled, stored.revision) == ("Welcome", True, 1)
    assert await get_proactive_recipe_preview_chars(db_session, GUILD) == 300


async def test_another_guilds_recipe_is_not_reachable(db_session):
    other = await add_recipe(db_session, guild_id=OTHER_GUILD)
    await _call(
        ProactiveRecipesAdminController.recipe_delete, db_session, recipe_id=other.id
    )
    assert await get_proactive_recipe(db_session, OTHER_GUILD, other.id) is not None
    assert await get_proactive_recipe(db_session, GUILD, uuid4()) is None


def test_the_sidebar_links_the_page():
    assert "/proactive-recipes" in admin_template_source("admin/bot/_sidebar.html")


# -- Privacy purge check -------------------------------------------------------------


async def test_the_purge_check_reports_a_recipe_naming_the_person(db_session):
    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis()
    named = await add_recipe(
        db_session, "Ping kai", f"When <@{KAI_ID}> posts a release, thank them."
    )
    await add_recipe(db_session, "Welcome")

    report = await scan_stores(db_session, redis, PurgeTarget.build(KAI_ID, ["kai"]))
    await redis.aclose()

    (hit,) = report["admin_written"]
    assert hit["store"] == "proactive_recipes"
    assert hit["recipe_id"] == str(named.id)
    assert hit["recipe_name"] == "Ping kai"
    assert hit["guild_id"] == GUILD
    assert hit["id_hits"] == 1
    assert hit_counts(report) == {"proactive_recipes": 1}
    # The purge never rewrites an admin's recipe.
    stored = await get_proactive_recipe(db_session, GUILD, named.id)
    assert KAI_ID in stored.instructions


def test_the_purge_page_lists_recipe_hits_with_a_link_to_edit():
    source = admin_template_source("admin/bot/privacy_purges/view.html")
    assert "report.admin_written" in source
    assert "/proactive-recipes/{{ hit.recipe_id }}/edit" in source


# -- Review #276 -------------------------------------------------------------------


def test_a_name_too_long_once_folded_for_matching_is_refused():
    # 80 characters as typed, 160 once ß folds to ss: over the name_key column.
    data = RecipeForm(name="ß" * 80, instructions="Do it.", enabled=True)
    (error,) = recipe_form_errors(data)
    assert "160" in error and "80" in error
    assert recipe_form_errors(RecipeForm("ß" * 40, "Do it.", True)) == []


async def test_the_form_refuses_a_name_too_long_once_folded(db_session):
    response = await _call(
        ProactiveRecipesAdminController.recipe_create,
        db_session,
        form={"name": "ß" * 80, "instructions": "Do it.", "action": "save"},
    )
    assert response.status_code == 400
    assert await list_proactive_recipes(db_session, GUILD) == []


async def test_two_edits_from_the_same_revision_both_count(db_session, test_engine):
    recipe = await add_recipe(db_session)
    other_session = async_sessionmaker(test_engine, expire_on_commit=False)()
    try:
        stale = await get_proactive_recipe(other_session, GUILD, recipe.id)
        assert stale.revision == 1
        await update_proactive_recipe(
            db_session,
            recipe,
            name="Welcome",
            name_key="welcome",
            instructions=WELCOME + "\n3. Wave.",
            enabled=True,
        )
        await db_session.commit()
        assert recipe.revision == 2
        # The other admin's page loaded revision 1; their save still counts.
        await update_proactive_recipe(
            other_session,
            stale,
            name="Welcome",
            name_key="welcome",
            instructions=WELCOME + "\n3. Smile.",
            enabled=True,
        )
        await other_session.commit()
        assert stale.revision == 3
    finally:
        await other_session.close()
    await db_session.refresh(recipe)
    assert recipe.revision == 3


async def test_the_preview_length_needs_a_guild_the_bot_is_in(db_session):
    patches = _admin_patches()
    for item in patches:
        item.start()
    try:
        with patch(
            f"{_CAMPAIGNS_MODULE}.get_admin_discord_client",
            return_value=SimpleNamespace(
                get_guild=AsyncMock(side_effect=GuildNotFoundError("gone"))
            ),
        ):
            response = await ProactiveRecipesAdminController.recipes_settings.fn(
                None,
                request=_Request({"preview_chars": "60"}),
                db_session=db_session,
                guild_id="999999999999999999",
            )
    finally:
        for item in reversed(patches):
            item.stop()
    assert response.status_code == 404
    assert (
        await get_proactive_recipe_preview_chars(db_session, "999999999999999999")
        == 300
    )


def test_the_form_trims_ascii_whitespace_only_as_the_preview_does():
    text = " Say hi. \n"
    data = read_recipe_form({"name": "Hi", "instructions": f"  {text}\r\n"})
    assert data.instructions == " Say hi. "
    assert recipe_preview(data.instructions, 300) == " Say hi. "


def test_the_closed_receipt_counts_recipe_hits():
    request = SimpleNamespace(
        steps={"guild_ids": [GUILD]},
        status="complete",
        check_report={"admin_written": [{"store": "proactive_recipes"}]},
    )
    assert receipt_summary(request)["admin_written"] == 1
