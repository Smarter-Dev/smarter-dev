"""The public chat-agent page and the member tags behind it (#103, #104).

What is pinned here is about what strangers may read:

- the masker turns every ``<id:name>`` tag, id-less ``<:name>`` tag, old
  ``name (id N)`` form, mention and bare known name (#108) into "a member",
  and the check (:func:`public_text_problem`) still runs on what is left;
- the page 404s until an admin switches one guild on, shows the real blocks
  masked, hides a block that still carries an id, a mention or HTML, leaves out lines about
  opted-out members, and shows a signed-in visitor their own tags only;
- the dream asks again for writing that names a known member outside a tag.

The model is always a stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
import yaml
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.template.config import TemplateConfig
from litestar.testing import AsyncTestClient
from pydantic_ai import ModelRetry
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.oauth_account import OAuthAccount
from skrift.markdown import render_markdown
from sqlalchemy import select

from smarter_dev.shared.config import get_settings
from smarter_dev.web import chat_agent_page_controller
from smarter_dev.web.bot_admin.chat_memory import ChatMemoryAdminController
from smarter_dev.web.chat_agent_page_controller import CHAT_AGENT_PATH
from smarter_dev.web.chat_agent_page_controller import chat_agent_page
from smarter_dev.web.chat_agent_public import check_public_blocks
from smarter_dev.web.chat_agent_public import member_names
from smarter_dev.web.chat_agent_public import public_block
from smarter_dev.web.chat_agent_public import public_guild_memory
from smarter_dev.web.chat_agent_public import public_text_problem
from smarter_dev.web.chat_agent_public import set_public_page
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_memory_dream import DreamContext
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import DreamOutput
from smarter_dev.web.chat_memory_dream import IdentityUpdate
from smarter_dev.web.chat_memory_dream import compose_blocks
from smarter_dev.web.chat_memory_dream import run_guild_dream
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.privacy_gate import load_gate
from tests.web.user_content_test import _user

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "themes" / "smarterdev"
CONTROLLER = "smarter_dev.web.chat_agent_page_controller:chat_agent_page"

KAI = "111111111111111111"
NIA = "222222222222222222"
_GUILD = "123456789012345678"
_OTHER_GUILD = "999999999999999999"
_CHANNEL = "555000111222333444"
_CUTOFF = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
_MORNING = _CUTOFF - timedelta(hours=15)

_BLOB = (
    "## People\n"
    f"- <{KAI}:kai> loves shaders\n"
    f"- <{NIA}:nia> runs the jam\n"
    "## Running topics\n"
    "Shader nights, the game jam, and whether tabs are a crime."
)
_BEHAVIOR = "Wait to be asked before explaining."
_PERSONALITY = "Dry, warm, and curious about what people are building."


# -- the check and the masker -----------------------------------------------------


def test_member_names_reads_tags_the_old_form_and_usernames():
    names = member_names(
        _BLOB,
        "zed.dev (id 42) waved at <:sam rose>",
        usernames=["Mallory", "", "x"],
    )

    assert names == {"kai", "nia", "zed.dev", "sam rose", "mallory"}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (f"someone ({KAI}) likes shaders", "it carries a Discord id"),
        ("the jam lead (id 7) is back", "it carries a Discord id"),
        ("<@&123> said hi", "it carries a mention"),
        ("Kai loves shaders", "it names a member"),
        ("the jam, as kai.", "it names a member"),
    ],
)
def test_the_check_refuses_ids_mentions_and_names(text, problem):
    assert public_text_problem(text, frozenset({"kai"})) == problem


@pytest.mark.parametrize(
    "html",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "bold</b>",
        "<!-- x -->",
    ],
)
def test_the_check_refuses_raw_html(html):
    assert (
        public_text_problem(f"Shader nights {html}", frozenset())
        == "it carries raw HTML"
    )


def test_the_check_matches_whole_names_only():
    names = frozenset({"kai"})

    assert public_text_problem("kaiju movies and Bokai tea", names) is None
    assert public_text_problem("a < b, and I <3 this place", names) is None


async def _block(db_session, raw: str, *, names=frozenset({"kai", "nia"})):
    return public_block(raw, names, await load_gate(db_session))


@pytest.mark.parametrize(
    "reference",
    [
        f"<{KAI}:kai>",
        "<:kai>",
        "<7:Big Kai>",
        f"kai (id {KAI})",
        "kai(id 7)",
        f"<@{KAI}>",
        f"<@!{KAI}>",
    ],
)
async def test_the_masker_masks_tags_both_forms_and_mentions(db_session, reference):
    block = await _block(db_session, f"- {reference} loves shaders")

    assert block.masked == "- a member loves shaders"
    assert block.problem is None


async def test_the_masker_leaves_emoji_and_timestamps_alone(db_session):
    block = await _block(db_session, "the jam <:party:123> starts <t:1:R>")

    assert block.masked == "the jam <:party:123> starts <t:1:R>"


async def test_a_name_left_outside_a_tag_is_masked(db_session):
    block = await _block(db_session, f"<{KAI}:kai> and nia run the jam")

    assert block.masked == "a member and a member run the jam"
    assert block.problem is None
    assert block.names_masked == ("nia",)


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("Ask Nia first.", "Ask a member first."),
        ("NIA and nIa and Nia.", "a member and a member and a member."),
        ("Nia's build is green.", "a member's build is green."),
        ("Kaiju and Bokai tea, not kai2.", "Kaiju and Bokai tea, not kai2."),
        ("#nia-chat is quiet.", "#a member-chat is quiet."),
    ],
    ids=["bare", "any case", "possessive", "whole words only", "channel"],
)
async def test_bare_known_names_are_masked_as_whole_words(db_session, raw, masked):
    block = await _block(db_session, raw)

    assert block.masked == masked
    assert block.problem is None


async def test_the_longest_name_is_masked_first(db_session):
    names = frozenset({"zech", "zech z", "z"})

    block = await _block(db_session, "Zech Z and Zech wrote it.", names=names)

    assert block.masked == "a member and a member wrote it."
    assert block.names_masked == ("zech", "zech z")


async def test_a_name_inside_a_tag_is_masked_once(db_session):
    block = await _block(db_session, f"<{KAI}:kai> <:Big Kai> kai (id 7) said hi")

    assert block.masked == "a member a member a member said hi"
    assert block.names_masked == ()


async def test_a_masked_bare_name_stays_masked_for_its_owner(db_session):
    block = await _block(db_session, f"kai and <{KAI}:kai> host")

    assert block.shown(frozenset({KAI})) == "a member and kai host"


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (f"Nia ({KAI}) hosts", "it carries a Discord id"),
        ("Nia pings <@&7>", "it carries a mention"),
        ("Nia says <b>hi</b>", "it carries raw HTML"),
    ],
)
async def test_masking_a_name_does_not_save_a_block_that_fails_otherwise(
    db_session, raw, problem
):
    block = await _block(db_session, raw)

    assert block.problem == problem
    assert block.shown() == ""


async def test_opted_out_lines_are_left_out_before_names_are_masked(db_session):
    await opt_out(db_session, KAI)

    block = await _block(db_session, f"- <{KAI}:kai> and Nia pair\n- Nia runs the jam")

    assert block.masked == "- a member runs the jam"
    assert block.lines_left_out == 1
    assert block.names_masked == ("nia",)


async def test_a_role_mention_is_not_a_member_and_hides_the_block(db_session):
    block = await _block(db_session, "ping <@&777777777777777777> for jams")

    assert block.masked == "ping <@&777777777777777777> for jams"
    assert block.problem is not None


async def test_lines_about_opted_out_members_are_left_out(db_session):
    await opt_out(db_session, KAI)

    block = await _block(db_session, _BLOB)

    assert "loves shaders" not in block.masked
    assert "- a member runs the jam" in block.masked
    assert block.lines_left_out == 1
    assert block.problem is None


async def test_an_id_less_tag_of_an_opted_out_member_is_left_out_by_name(db_session):
    await opt_out(db_session, KAI)
    memory = await _seed_memory(
        db_session, content=f"- <{KAI}:kai> loves shaders\n- <:kai> hosts shader night"
    )

    blocks = await check_public_blocks(db_session, memory)

    assert "shader night" not in blocks["memory"].masked
    assert blocks["memory"].lines_left_out == 2


async def test_the_viewer_sees_their_own_tags_and_nobody_elses(db_session):
    block = await _block(
        db_session, f"<{KAI}:kai> and <{NIA}:nia> and <@{KAI}> and <:kai>"
    )

    assert block.shown(frozenset({KAI})) == "kai and a member and you and a member"
    assert block.shown(frozenset({NIA})) == "a member and nia and a member and a member"
    assert block.shown() == "a member and a member and a member and a member"


# -- the opt-out gate on the tag form ---------------------------------------------


async def test_the_gate_redacts_an_opted_out_members_tag_whole(db_session):
    await opt_out(db_session, KAI)
    gate = await load_gate(db_session)

    text = f"<{KAI}:kai> and <{NIA}:nia> and <:kai>"

    assert gate.redact(text) == f"[blocked user] and <{NIA}:nia> and <:kai>"
    assert gate.carries_blocked_id(f"<{KAI}:kai> waved")
    assert not gate.carries_blocked_id("<:kai> waved")
    assert gate.carries_blocked_member("<:kai> waved", frozenset({"kai"}))
    assert gate.blocked_names_in(_BLOB, f"kai2 (id {KAI})") == {"kai", "kai2"}


# -- the dream ------------------------------------------------------------------


def _context(previous_blob: str = "", **kwargs) -> DreamContext:
    return DreamContext(previous_blob=previous_blob, notes=["a note"], **kwargs)


def test_the_dream_asks_again_for_an_untagged_name():
    output = DreamOutput(memory=f"<{KAI}:kai> is back; kai shipped it")

    with pytest.raises(ModelRetry, match="names kai outside a tag"):
        compose_blocks(output, _context(), retries_left=1)


@pytest.mark.parametrize(
    "memory",
    [f"kai (id {KAI}) is back", "mallory is back", "Mallory is back"],
)
def test_the_old_form_and_known_names_are_untagged_too(memory):
    context = _context(member_names=frozenset({"mallory"}))

    with pytest.raises(ModelRetry, match="outside a tag"):
        compose_blocks(DreamOutput(memory=memory), context, retries_left=1)


@pytest.mark.parametrize(
    "memory",
    [f"<{KAI}:kai> is back", "<:kai> is back", "kaiju night is back"],
)
def test_tags_with_and_without_an_id_pass(memory):
    context = _context(member_names=frozenset({"kai"}))

    blocks = compose_blocks(DreamOutput(memory=memory), context, retries_left=1)

    assert blocks.memory == memory


def test_a_revised_block_and_a_new_identity_trait_are_checked():
    context = _context(member_names=frozenset({"kai"}), previous_behavior="Be kind.")

    with pytest.raises(ModelRetry, match="`behavior` names kai"):
        compose_blocks(
            DreamOutput(memory="m", behavior="Be kind. Answer kai first."),
            context,
            retries_left=1,
        )
    with pytest.raises(ModelRetry, match="`memory` names kai"):
        compose_blocks(
            DreamOutput(
                memory="m",
                identity_updates=[
                    IdentityUpdate(after="I tease kai.", evidence="a note")
                ],
            ),
            context,
            retries_left=1,
        )


def test_blocks_the_dream_leaves_alone_keep_the_old_form():
    context = _context(
        member_names=frozenset({"kai"}),
        previous_blob="## Identity & Voice\n- I owe kai (id 7) a rematch.",
        previous_behavior="Answer kai (id 7) first.",
        previous_personality="kai (id 7) taught me shaders.",
    )

    blocks = compose_blocks(
        DreamOutput(memory="- <7:kai> is back"), context, retries_left=1
    )

    assert blocks.behavior == "Answer kai (id 7) first."
    assert "kai (id 7) a rematch" in blocks.memory


def test_on_the_last_attempt_the_night_stands():
    context = _context(member_names=frozenset({"kai"}))

    blocks = compose_blocks(DreamOutput(memory="kai is back"), context, retries_left=0)

    assert blocks.memory == "kai is back" and blocks.refusals == ()


@dataclass
class _Dream:
    memory: str
    contexts: list[DreamContext] = field(default_factory=list)

    async def run(self, user_prompt: str, *, deps: DreamContext):
        self.contexts.append(deps)
        return type("Result", (), {"output": DreamOutput(memory=self.memory)})()


async def _note(session, content: str):
    return await create_memory_note(
        session,
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        channel_name="dev-help",
        content=content,
        created_at=_MORNING,
        day_start=_MORNING.replace(hour=0),
    )


async def _seed_memory(session, *, guild_id: str = _GUILD, content: str = _BLOB):
    await upsert_guild_memory_blob(
        session,
        guild_id=guild_id,
        content=content,
        behavior=_BEHAVIOR,
        personality=_PERSONALITY,
        notes_consumed=0,
        model_name="stub",
        dreamed_at=_CUTOFF - timedelta(days=1),
    )
    await session.commit()
    return await get_guild_memory_blob(session, guild_id)


async def _dream(session, agent):
    return await run_guild_dream(
        session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_CUTOFF + timedelta(minutes=20),
        agent=agent,
    )


async def test_engagement_usernames_and_opted_out_names_are_known(db_session):
    await _seed_memory(db_session)
    await _note(db_session, f"<{NIA}:nia> shipped the jam build")
    db_session.add(
        ChatAgentEngagement(
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            activation_user_id="333333333333333333",
            activation_username="mallory",
            activation_message_id="444444444444444444",
        )
    )
    await db_session.commit()
    await opt_out(db_session, KAI)
    agent = _Dream(memory=f"## People\n- <{NIA}:nia> shipped")

    result = await _dream(db_session, agent)

    assert result.outcome is DreamOutcome.DREAMED
    assert KAI not in agent.contexts[0].previous_blob
    assert {"kai", "nia", "mallory"} <= agent.contexts[0].member_names


async def test_an_id_less_note_about_an_opted_out_member_is_not_read(db_session):
    await _seed_memory(db_session)
    await _note(db_session, "<:kai> is hosting shader night")
    await _note(db_session, f"<{NIA}:nia> shipped the jam build")
    await db_session.commit()
    await opt_out(db_session, KAI)
    agent = _Dream(memory=f"## People\n- <{NIA}:nia> shipped")

    await _dream(db_session, agent)

    assert agent.contexts[0].notes == [f"<{NIA}:nia> shipped the jam build"]


# -- the switch -------------------------------------------------------------------


async def test_the_switch_is_off_by_default_and_shows_one_guild(db_session):
    first = await _seed_memory(db_session)
    second = await _seed_memory(db_session, guild_id=_OTHER_GUILD)
    assert await public_guild_memory(db_session) is None

    await set_public_page(db_session, first, enabled=True)
    await db_session.commit()
    assert (await public_guild_memory(db_session)).guild_id == _GUILD

    await set_public_page(db_session, second, enabled=True)
    await db_session.commit()
    rows = (await db_session.scalars(select(ChatAgentGuildMemory))).all()
    assert {r.guild_id for r in rows if r.public_page_enabled} == {_OTHER_GUILD}

    await set_public_page(db_session, second, enabled=False)
    await db_session.commit()
    assert await public_guild_memory(db_session) is None


async def test_a_dream_never_flips_the_switch(db_session):
    memory = await _seed_memory(db_session)
    await set_public_page(db_session, memory, enabled=True)
    await db_session.commit()

    await _seed_memory(db_session)

    await db_session.refresh(memory)
    assert memory.public_page_enabled is True


def _admin_request(form: dict):
    request = AsyncMock()
    request.form = AsyncMock(return_value=form)
    return request


async def _post_switch(db_session, form: dict, *, csrf: bool = True):
    with (
        patch(
            "smarter_dev.web.bot_admin.chat_memory.verify_csrf",
            new=AsyncMock(return_value=csrf),
        ),
        patch("smarter_dev.web.bot_admin.chat_memory.flash_success") as success,
        patch("smarter_dev.web.bot_admin.chat_memory.flash_error") as error,
    ):
        response = await ChatMemoryAdminController.chat_memory_public_page.fn(
            None, request=_admin_request(form), db_session=db_session, guild_id=_GUILD
        )
    return response, success, error


async def test_the_admin_switch_turns_the_page_on_and_off(db_session):
    await _seed_memory(db_session)

    response, success, _ = await _post_switch(db_session, {"enabled": "on"})
    assert response.url == f"/admin/bot/guilds/{_GUILD}/chat-memory"
    assert success.called
    assert (await public_guild_memory(db_session)).guild_id == _GUILD

    await _post_switch(db_session, {})
    assert await public_guild_memory(db_session) is None


async def test_the_admin_switch_needs_a_valid_form_and_a_memory(db_session):
    _, _, error = await _post_switch(db_session, {"enabled": "on"})
    assert error.called and await public_guild_memory(db_session) is None

    await _seed_memory(db_session)
    _, _, error = await _post_switch(db_session, {"enabled": "on"}, csrf=False)
    assert error.called and await public_guild_memory(db_session) is None


def test_the_admin_preview_shows_raw_beside_masked_and_flags_what_hides():
    environment = Environment(
        loader=FileSystemLoader(REPO / "templates"), autoescape=True
    )
    source = environment.loader.get_source(
        environment, "admin/bot/chat_memory/view.html"
    )[0]

    assert "block.raw" in source and "block.masked" in source
    assert "hidden on the page: {{ block.problem }}" in source
    assert "public_content" not in source


# -- the page ------------------------------------------------------------------


def _environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(THEME / "templates"), autoescape=True
    )
    environment.globals.update(
        theme_url=lambda path: f"/theme/{path}",
        static_url=lambda path: f"/static/{path}",
        csp_nonce=lambda: "",
    )
    environment.filters["markdown"] = render_markdown
    return environment


@pytest.fixture(autouse=True)
def _fresh_blocks_cache():
    chat_agent_page_controller._blocks_cache.clear()
    yield
    chat_agent_page_controller._blocks_cache.clear()


@pytest.fixture
async def client(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_base_url", "https://smarter.dev")

    async def session():
        return db_session

    session_config = CookieBackendConfig(secret=b"0" * 16)
    app = Litestar(
        route_handlers=[chat_agent_page],
        dependencies={"db_session": Provide(session)},
        middleware=[session_config.middleware],
        template_config=TemplateConfig(
            instance=JinjaTemplateEngine.from_environment(_environment())
        ),
    )
    async with AsyncTestClient(app, session_config=session_config) as c:
        yield c


async def _switch_on(db_session, **fields):
    memory = await get_guild_memory_blob(db_session, _GUILD)
    for key, value in fields.items():
        setattr(memory, key, value)
    await set_public_page(db_session, memory, enabled=True)
    await db_session.commit()


async def _sign_in(db_session, client, *discord_ids: str):
    user = await _user(db_session)
    for discord_id in discord_ids:
        db_session.add(
            OAuthAccount(
                provider="discord",
                provider_account_id=discord_id,
                provider_email=None,
                provider_email_verified=False,
                user_id=user.id,
            )
        )
    await db_session.commit()
    await client.set_session_data({SESSION_USER_ID: str(user.id)})


async def test_the_page_is_404_while_switched_off(db_session, client):
    assert (await client.get(CHAT_AGENT_PATH)).status_code == 404

    await _seed_memory(db_session)
    assert (await client.get(CHAT_AGENT_PATH)).status_code == 404


async def test_the_page_shows_the_real_blocks_masked(db_session, client):
    await _seed_memory(db_session)
    await _switch_on(db_session)

    response = await client.get(CHAT_AGENT_PATH)

    assert response.status_code == 200
    html = response.text
    assert "Conversations mode (the default)" in html and "Assistant mode." in html
    assert _PERSONALITY in html and _BEHAVIOR in html
    assert "a member loves shaders" in html and "a member runs the jam" in html
    assert "whether tabs are a crime" in html
    assert "Last dream: Aug 06, 2026 at 00:00 UTC" in html
    assert '<link rel="canonical" href="https://smarter.dev/chat-agent">' in html
    for private in (KAI, NIA, "kai", "nia"):
        assert private not in html.casefold().replace("a member", "")
    assert "cache-control" not in response.headers


async def test_before_the_first_dream_the_page_says_so(db_session, client):
    await _seed_memory(db_session, content="")
    await _switch_on(db_session)

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "has not dreamed its memory yet" in html


@pytest.mark.parametrize(
    ("field_name", "bad", "shown_text"),
    [
        ("personality", "I like <@&7> and kai a lot.", "I like"),
        ("behavior", "Never tease <@&777777777777777777>.", "Never tease"),
        ("behavior", f"Answer {KAI} first.", "Answer"),
    ],
)
async def test_a_block_that_still_fails_after_masking_is_hidden(
    db_session, client, field_name, bad, shown_text
):
    await _seed_memory(db_session)
    await _switch_on(db_session, **{field_name: bad})

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert shown_text not in html
    label = "memory" if field_name == "content" else field_name
    assert f"The {label} is not shown right now." in html
    assert "Tonight" not in html


async def test_a_signed_in_viewer_sees_only_their_own_tags(db_session, client):
    await _seed_memory(db_session)
    await _switch_on(db_session)

    await _sign_in(db_session, client, KAI)
    response = await client.get(CHAT_AGENT_PATH)
    assert "kai loves shaders" in response.text
    assert "a member runs the jam" in response.text
    assert "nia" not in response.text.casefold().replace("a member", "")
    assert response.headers["cache-control"] == "private, no-store"

    await _sign_in(db_session, client, NIA, "333333333333333333")
    html = (await client.get(CHAT_AGENT_PATH)).text
    assert "nia runs the jam" in html and "a member loves shaders" in html
    assert "kai loves" not in html

    client.cookies.clear()
    html = (await client.get(CHAT_AGENT_PATH)).text
    assert "a member loves shaders" in html and "a member runs the jam" in html


async def test_a_signed_in_viewer_without_discord_sees_everyone_masked(
    db_session, client
):
    await _seed_memory(db_session)
    await _switch_on(db_session)
    await _sign_in(db_session, client)

    response = await client.get(CHAT_AGENT_PATH)

    assert "a member loves shaders" in response.text
    assert "cache-control" not in response.headers


async def test_the_cache_holds_masked_blocks_never_a_viewers_render(
    db_session, client, monkeypatch
):
    await _seed_memory(db_session)
    await _switch_on(db_session)
    real = chat_agent_page_controller.check_public_blocks
    calls = []

    async def counting(session, memory):
        calls.append(memory.content)
        return await real(session, memory)

    monkeypatch.setattr(chat_agent_page_controller, "check_public_blocks", counting)

    await _sign_in(db_session, client, KAI)
    assert "kai loves shaders" in (await client.get(CHAT_AGENT_PATH)).text
    client.cookies.clear()
    assert "a member loves shaders" in (await client.get(CHAT_AGENT_PATH)).text
    assert len(calls) == 1
    for _, blocks in chat_agent_page_controller._blocks_cache.values():
        assert "kai" not in blocks["memory"].masked

    await _switch_on(db_session, content="Shader nights, now weekly.")
    assert "now weekly" in (await client.get(CHAT_AGENT_PATH)).text
    assert len(calls) == 2

    memory = await get_guild_memory_blob(db_session, _GUILD)
    await set_public_page(db_session, memory, enabled=False)
    await db_session.commit()
    assert (await client.get(CHAT_AGENT_PATH)).status_code == 404


_INJECTION = '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">'


async def test_raw_html_in_a_block_is_hidden(db_session, client):
    await _seed_memory(db_session, content=_INJECTION)
    await _switch_on(db_session, personality=f"Dry. {_INJECTION}")

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "alert(1)" not in html
    assert "The memory is not shown right now." in html
    assert "The personality is not shown right now." in html


async def test_raw_html_that_got_past_the_check_renders_as_text(
    db_session, client, monkeypatch
):
    # The backstop: the page's markdown renderer escapes raw HTML.
    monkeypatch.setattr(
        "smarter_dev.web.chat_agent_public.public_text_problem",
        lambda text, names: None,
    )
    await _seed_memory(db_session)
    await _switch_on(db_session, behavior=_INJECTION)

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html and "<img src=x" not in html


@pytest.mark.parametrize("config", ["app.yaml", "app.development.yaml"])
def test_the_route_is_registered(config):
    controllers = yaml.safe_load((REPO / config).read_text())["controllers"]
    assert CONTROLLER in controllers


# -- the model -----------------------------------------------------------------


def test_the_model_has_no_public_block():
    assert "public_content" not in ChatAgentGuildMemory.__table__.columns
    assert "public_page_enabled" in ChatAgentGuildMemory.__table__.columns
