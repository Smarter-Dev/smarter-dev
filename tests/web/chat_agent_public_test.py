"""The public chat-agent page and the public memory block behind it (#103).

Three things are pinned here, all about what strangers may read:

- the check (:func:`public_text_problem`) refuses ids, mentions and the names
  of members the agent knows;
- the dream refuses a public block that fails it, asks again, and on the last
  attempt falls back to yesterday's block or none, without costing the night;
- the page 404s until an admin switches one guild on, never shows the memory
  block, and hides a block that fails the check or names an opted-out member.

The model is always a stub.
"""

from __future__ import annotations

import importlib.util
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
from skrift.markdown import render_markdown
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from smarter_dev.shared.config import get_settings
from smarter_dev.web import chat_agent_page_controller
from smarter_dev.web.bot_admin.chat_memory import ChatMemoryAdminController
from smarter_dev.web.chat_agent_page_controller import CHAT_AGENT_PATH
from smarter_dev.web.chat_agent_page_controller import chat_agent_page
from smarter_dev.web.chat_agent_public import member_names
from smarter_dev.web.chat_agent_public import public_guild_memory
from smarter_dev.web.chat_agent_public import public_text_problem
from smarter_dev.web.chat_agent_public import set_public_page
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_memory_dream import DreamContext
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import DreamOutput
from smarter_dev.web.chat_memory_dream import resolve_public_memory
from smarter_dev.web.chat_memory_dream import run_guild_dream
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentGuildMemory

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

_BLOB = f"## People\n- kai (id {KAI}) loves shaders\n- nia (id {NIA}) runs the jam"
_PUBLIC = (
    "## Running topics\nShader nights, the game jam, and whether tabs are a crime."
)


# -- the check -------------------------------------------------------------------


def test_member_names_reads_the_dreams_naming_form_and_usernames():
    names = member_names(_BLOB, "zed.dev (id 42) waved", usernames=["Mallory", "", "x"])

    assert names == {"kai", "nia", "zed.dev", "mallory"}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (f"someone ({KAI}) likes shaders", "it carries a Discord id"),
        ("the jam lead (id 7) is back", "it carries a Discord id"),
        ("<@!123> said hi", "it carries a mention"),
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


def test_the_check_allows_angle_brackets_that_are_not_html():
    assert public_text_problem("a < b, and I <3 this place", frozenset()) is None


def test_the_check_matches_whole_names_only():
    names = frozenset({"kai"})

    assert public_text_problem("kaiju movies and Bokai tea", names) is None
    assert public_text_problem(_PUBLIC, names) is None
    assert public_text_problem("Rust 1.92 ships in 6 weeks", names) is None


# -- the dream ------------------------------------------------------------------


def _context(**kwargs) -> DreamContext:
    return DreamContext(previous_blob=_BLOB, notes=["a note"], **kwargs)


def test_a_public_block_naming_someone_is_asked_for_again():
    output = DreamOutput(memory="m", public_memory="kai had a good week")

    with pytest.raises(ModelRetry, match="names a member"):
        resolve_public_memory(
            output, _context(member_names=frozenset({"kai"})), memory="", retries_left=1
        )


def test_a_name_only_tonights_memory_carries_is_refused_too():
    output = DreamOutput(memory="m", public_memory="zed shipped a thing")

    with pytest.raises(ModelRetry, match="names a member"):
        resolve_public_memory(
            output, _context(), memory="- zed (id 77) shipped", retries_left=1
        )


@pytest.mark.parametrize("bad", ["<@1> is great", f"id {KAI} posted"])
def test_ids_and_mentions_are_asked_for_again(bad):
    with pytest.raises(ModelRetry):
        resolve_public_memory(
            DreamOutput(memory="m", public_memory=bad),
            _context(),
            memory="",
            retries_left=1,
        )


def test_on_the_last_attempt_a_failing_block_falls_back_to_yesterdays():
    output = DreamOutput(memory="m", public_memory="kai had a good week")
    context = _context(member_names=frozenset({"kai"}), previous_public=_PUBLIC)

    assert resolve_public_memory(output, context, memory="", retries_left=0) == _PUBLIC


def test_yesterdays_block_is_dropped_too_when_it_now_names_someone():
    output = DreamOutput(memory="m", public_memory="kai had a good week")
    context = _context(
        member_names=frozenset({"kai"}), previous_public="kai and the jam"
    )

    assert resolve_public_memory(output, context, memory="", retries_left=0) == ""


def test_an_omitted_block_keeps_yesterdays():
    context = _context(previous_public=_PUBLIC)

    assert (
        resolve_public_memory(
            DreamOutput(memory="m"), context, memory="", retries_left=1
        )
        == _PUBLIC
    )


def test_an_over_long_block_is_asked_for_again_then_cut():
    long = "\n".join(["Shader nights are back."] * 200)
    output = DreamOutput(memory="m", public_memory=long)

    with pytest.raises(ModelRetry):
        resolve_public_memory(output, _context(), memory="", retries_left=1)
    cut = resolve_public_memory(output, _context(), memory="", retries_left=0)
    assert 0 < len(cut) <= MAX_MEMORY_BLOB_CHARS


@dataclass
class _Dream:
    memory: str
    public_memory: str | None
    contexts: list[DreamContext] = field(default_factory=list)

    async def run(self, user_prompt: str, *, deps: DreamContext):
        self.contexts.append(deps)
        output = DreamOutput(memory=self.memory, public_memory=self.public_memory)
        return type("Result", (), {"output": output})()


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


async def _seed_memory(session, *, public: str = "", guild_id: str = _GUILD):
    await upsert_guild_memory_blob(
        session,
        guild_id=guild_id,
        content=_BLOB,
        behavior="Wait to be asked before explaining.",
        personality="Dry, warm, and curious about what people are building.",
        public_content=public,
        notes_consumed=0,
        model_name="stub",
        dreamed_at=_CUTOFF - timedelta(days=1),
    )
    await session.commit()


async def _dream(session, agent):
    return await run_guild_dream(
        session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_CUTOFF + timedelta(minutes=20),
        agent=agent,
    )


async def test_the_dream_saves_a_clean_public_block(db_session):
    await _seed_memory(db_session)
    await _note(db_session, f"nia (id {NIA}) shipped the jam build")
    await db_session.commit()

    result = await _dream(
        db_session,
        _Dream(memory=f"## People\n- nia (id {NIA}) shipped", public_memory=_PUBLIC),
    )

    assert result.outcome is DreamOutcome.DREAMED
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.public_content == _PUBLIC
    assert memory.public_page_enabled is False


async def test_a_refused_public_block_does_not_cost_the_night(db_session):
    await _seed_memory(db_session, public=_PUBLIC)
    await _note(db_session, f"nia (id {NIA}) shipped the jam build")
    await db_session.commit()
    new_memory = f"## People\n- nia (id {NIA}) shipped the jam build"

    result = await _dream(
        db_session, _Dream(memory=new_memory, public_memory="nia shipped the jam build")
    )

    assert result.outcome is DreamOutcome.DREAMED
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == new_memory
    assert memory.public_content == _PUBLIC


async def test_an_opted_out_members_name_is_known_though_the_model_never_saw_it(
    db_session,
):
    await _seed_memory(db_session)
    await _note(db_session, f"nia (id {NIA}) shipped the jam build")
    await db_session.commit()
    await opt_out(db_session, KAI)
    agent = _Dream(
        memory=f"## People\n- nia (id {NIA}) shipped",
        public_memory="Kai's shaders rule.",
    )

    await _dream(db_session, agent)

    assert KAI not in agent.contexts[0].previous_blob
    assert "kai" in agent.contexts[0].member_names
    assert (await get_guild_memory_blob(db_session, _GUILD)).public_content == ""


async def test_engagement_usernames_are_known_names(db_session):
    await _seed_memory(db_session)
    await _note(db_session, "a quiet day in the jam channel")
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

    await _dream(
        db_session,
        _Dream(
            memory=f"## People\n- nia (id {NIA}) here", public_memory="mallory won."
        ),
    )

    assert (await get_guild_memory_blob(db_session, _GUILD)).public_content == ""


# -- the switch -------------------------------------------------------------------


async def test_the_switch_is_off_by_default_and_shows_one_guild(db_session):
    await _seed_memory(db_session)
    await _seed_memory(db_session, guild_id=_OTHER_GUILD)
    assert await public_guild_memory(db_session) is None

    first = await get_guild_memory_blob(db_session, _GUILD)
    second = await get_guild_memory_blob(db_session, _OTHER_GUILD)
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
    await _seed_memory(db_session)
    await set_public_page(
        db_session, await get_guild_memory_blob(db_session, _GUILD), enabled=True
    )
    await db_session.commit()

    await _seed_memory(db_session, public=_PUBLIC)

    memory = await get_guild_memory_blob(db_session, _GUILD)
    await db_session.refresh(memory)
    assert memory.public_page_enabled is True
    assert memory.public_content == _PUBLIC


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

    app = Litestar(
        route_handlers=[chat_agent_page],
        dependencies={"db_session": Provide(session)},
        middleware=[CookieBackendConfig(secret=b"0" * 16).middleware],
        template_config=TemplateConfig(
            instance=JinjaTemplateEngine.from_environment(_environment())
        ),
    )
    async with AsyncTestClient(app) as c:
        yield c


async def _switch_on(db_session, **fields):
    memory = await get_guild_memory_blob(db_session, _GUILD)
    for key, value in fields.items():
        setattr(memory, key, value)
    await set_public_page(db_session, memory, enabled=True)
    await db_session.commit()


async def test_the_page_is_404_while_switched_off(db_session, client):
    assert (await client.get(CHAT_AGENT_PATH)).status_code == 404

    await _seed_memory(db_session, public=_PUBLIC)
    assert (await client.get(CHAT_AGENT_PATH)).status_code == 404


async def test_the_page_shows_the_blocks_never_the_memory(db_session, client):
    await _seed_memory(db_session, public=_PUBLIC)
    await _switch_on(db_session)

    response = await client.get(CHAT_AGENT_PATH)

    assert response.status_code == 200
    html = response.text
    assert "Conversations mode (the default)" in html and "Assistant mode." in html
    assert "Dry, warm, and curious" in html
    assert "Wait to be asked before explaining." in html
    assert "whether tabs are a crime" in html
    assert "Last dream: Aug 06, 2026 at 00:00 UTC" in html
    assert 'href="/privacy"' in html
    assert '<link rel="canonical" href="https://smarter.dev/chat-agent">' in html
    for private in (KAI, NIA, "loves shaders", "runs the jam"):
        assert private not in html


async def test_before_the_first_public_dream_the_page_says_so(db_session, client):
    await _seed_memory(db_session)
    await _switch_on(db_session)

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "has not dreamed its public memory yet" in html


@pytest.mark.parametrize(
    ("field_name", "bad", "shown_text"),
    [
        ("personality", "I like kai a lot.", "I like kai"),
        ("behavior", f"Never tease <@{NIA}>.", "Never tease"),
        ("public_content", "nia runs the jam and it is great.", "it is great"),
        ("behavior", f"Answer {KAI} first.", "Answer"),
    ],
)
async def test_a_block_that_fails_the_check_is_hidden(
    db_session, client, field_name, bad, shown_text
):
    await _seed_memory(db_session, public=_PUBLIC)
    await _switch_on(db_session, **{field_name: bad})

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert shown_text not in html
    if field_name == "public_content":
        assert "public memory is not shown here" in html


async def test_a_block_naming_an_opted_out_member_by_id_is_hidden(db_session, client):
    # The id check refuses this already; the opt-out gate behind it is a
    # second lock that, today, never sees an id the first one missed.
    await _seed_memory(db_session, public=_PUBLIC)
    await opt_out(db_session, KAI)
    await _switch_on(db_session, personality=f"kai ({KAI}) taught me shaders")

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "taught me shaders" not in html and KAI not in html


@pytest.mark.parametrize("config", ["app.yaml", "app.development.yaml"])
def test_the_route_is_registered(config):
    controllers = yaml.safe_load((REPO / config).read_text())["controllers"]
    assert CONTROLLER in controllers


# -- migration -----------------------------------------------------------------


def test_migration_adds_an_empty_public_block_and_an_off_switch(tmp_path):
    path = next((REPO / "alembic" / "main" / "versions").glob("*_b4e8a1d6c2f9_*.py"))
    spec = importlib.util.spec_from_file_location("public_block_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE chat_agent_guild_memory (id CHAR(32) PRIMARY KEY, "
                "guild_id VARCHAR(20) NOT NULL, content VARCHAR(2000) NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO chat_agent_guild_memory VALUES ('a', :g, :c)"),
            {"g": _GUILD, "c": _BLOB},
        )
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()

        row = connection.execute(
            text(
                "SELECT content, public_content, public_page_enabled "
                "FROM chat_agent_guild_memory"
            )
        ).one()
        assert row == (_BLOB, "", 0)

        revision.downgrade()
        columns = {
            c["name"]
            for c in inspect(connection).get_columns("chat_agent_guild_memory")
        }
        assert columns == {"id", "guild_id", "content"}
    engine.dispose()


_INJECTION = '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">'


async def test_raw_html_in_a_block_is_hidden(db_session, client):
    await _seed_memory(db_session, public=_INJECTION)
    await _switch_on(db_session, personality=f"Dry. {_INJECTION}")

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "alert(1)" not in html
    assert "public memory is not shown here" in html


async def test_raw_html_that_got_past_the_check_renders_as_text(
    db_session, client, monkeypatch
):
    # The backstop: the page's markdown renderer escapes raw HTML.
    monkeypatch.setattr(
        "smarter_dev.web.chat_agent_public.public_text_problem",
        lambda text, names: None,
    )
    await _seed_memory(db_session, public=_INJECTION)
    await _switch_on(db_session, behavior=_INJECTION)

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html and "<img src=x" not in html


async def test_the_checked_blocks_are_cached_until_a_block_changes(
    db_session, client, monkeypatch
):
    await _seed_memory(db_session, public=_PUBLIC)
    await _switch_on(db_session)
    real = chat_agent_page_controller.check_public_blocks
    calls = []

    async def counting(session, memory):
        calls.append(memory.public_content)
        return await real(session, memory)

    monkeypatch.setattr(chat_agent_page_controller, "check_public_blocks", counting)

    await client.get(CHAT_AGENT_PATH)
    await client.get(CHAT_AGENT_PATH)
    assert len(calls) == 1

    await _switch_on(db_session, public_content="Shader nights, now weekly.")
    assert "now weekly" in (await client.get(CHAT_AGENT_PATH)).text
    assert len(calls) == 2

    memory = await get_guild_memory_blob(db_session, _GUILD)
    await set_public_page(db_session, memory, enabled=False)
    await db_session.commit()
    assert (await client.get(CHAT_AGENT_PATH)).status_code == 404
