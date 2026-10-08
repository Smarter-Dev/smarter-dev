"""Bot-side memory writers refuse or blank opted-out members (#100).

Every writer that keeps something about or from a member checks the shared
gate (``smarter_dev.bot.privacy.gate``): the ``remember`` tool, the chat
history, topic and notes, and the guild memory as it is loaded into a prompt.
Each test has a negative control: the same input about someone not on the
list goes through unchanged. Synthetic members only: kai (opted out) and nia.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.agents.chat_tools import REMEMBER_OPTED_OUT
from smarter_dev.bot.agents.chat_tools import REMEMBER_SAVED
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import remember
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.privacy.gate import blank_chat_text
from smarter_dev.bot.privacy.gate import blank_model_messages
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.bot.services.guild_chat_memory_service import GuildChatMemoryService
from smarter_dev.shared.privacy_purge import BLOCKED_PLACEHOLDER

KAI = "111111111111111111"
NIA = "222222222222222222"
BOT = "999999999999999999"


@pytest.fixture
def kai_opted_out():
    blocked = get_blocked_users()
    blocked.load(1, [KAI])
    return blocked


# -- the remember tool ----------------------------------------------------------


class _API:
    def __init__(self, result: dict | None = None):
        self.result = result or {"saved": True}
        self.posts: list[dict] = []

    async def post(self, path, json_data=None, **_kwargs):
        self.posts.append(json_data)
        return SimpleNamespace(json=lambda: self.result)


def _ctx(api, *, members: dict[str, SimpleNamespace] | None = None, **deps):
    bot = MagicMock()
    bot.cache.get_member = lambda guild_id, user_id: (members or {}).get(str(user_id))
    return SimpleNamespace(
        deps=ChatDeps(bot=bot, channel_id=4242, guild_id=99, api_client=api, **deps)
    )


@pytest.mark.parametrize(
    "note",
    [f"kai (id {KAI}) loves shaders", f"<@{KAI}> wound me up again"],
    ids=["by-id", "by-mention"],
)
async def test_remember_refuses_a_note_carrying_an_opted_out_id(kai_opted_out, note):
    api = _API()

    assert await remember(_ctx(api), note) == REMEMBER_OPTED_OUT
    assert api.posts == []

    # Control: the same note about someone not on the list is kept, and the
    # web app is told who it is about.
    api = _API()
    assert await remember(_ctx(api), note.replace(KAI, NIA)) == REMEMBER_SAVED
    assert api.posts[0]["about_user_ids"] == [NIA]


async def test_remember_refuses_a_note_naming_an_opted_out_member(kai_opted_out):
    members = {KAI: SimpleNamespace(username="kaiwren", global_name=None, nickname="Kai W")}
    api = _API()

    assert (
        await remember(_ctx(api, members=members), "kaiwren beat me at chess")
        == REMEMBER_OPTED_OUT
    )
    assert api.posts == []

    # Control: a name no blocked member goes by.
    assert (
        await remember(_ctx(api, members=members), "nia beat me at chess") == REMEMBER_SAVED
    )


async def test_remember_refuses_once_someone_the_turn_read_opts_out():
    blocked = get_blocked_users()
    api = _API()
    ctx = _ctx(api, source_user_ids=frozenset({KAI, NIA}))

    assert await remember(ctx, "a good day in here") == REMEMBER_SAVED  # control
    assert api.posts[0]["about_user_ids"] == [KAI, NIA]

    blocked.block_now(KAI, 1)
    assert await remember(ctx, "another good day in here") == REMEMBER_OPTED_OUT
    assert len(api.posts) == 1


async def test_the_servers_opted_out_refusal_reads_as_the_same_sentence():
    api = _API({"saved": False, "reason": "opted_out"})
    assert await remember(_ctx(api), "a thought") == REMEMBER_OPTED_OUT


# -- chat history, topic and notes --------------------------------------------------


def _rendered(author: str, body: str, *, reply_to: str | None = None) -> str:
    attrs = f'id="1" sent-utc="2026-10-07T22:57:42Z" user-id="{author}" username="u{author[:2]}"'
    if reply_to:
        attrs += f' reply-to="2" reply-to-user-id="{reply_to}" reply-to-username="u{reply_to[:2]}"'
    return f"<message {attrs}>\n{body}\n</message>"


def test_blank_chat_text_blanks_the_author_and_unnames_replies_to_them(kai_opted_out):
    text = "\n".join(
        [
            _rendered(KAI, "kai's words"),
            _rendered(NIA, "nia agrees", reply_to=KAI),
            _rendered(NIA, "nia's own words"),
            f'<message id="3" sent-utc="2026-10-07T22:58:00Z" self reply-to="1" '
            f'reply-to-user-id="{KAI}">\nthe bot answering kai\n</message>',
        ]
    )

    out = blank_chat_text(text)

    assert "kai's words" not in out and KAI not in out and f"u{KAI[:2]}" not in out
    assert out.startswith(BLOCKED_PLACEHOLDER + "\n")
    assert "nia agrees" in out and "nia's own words" in out
    # The bot's own message replying to kai is not kai's message.
    assert "the bot answering kai" in out
    # Control: with kai not on the list nothing changes.
    get_blocked_users().load(2, [])
    assert blank_chat_text(text) == text


async def test_chat_history_is_written_with_the_opted_out_member_blanked(kai_opted_out):
    memory = ChatMemory(fakeredis.aioredis.FakeRedis())
    history = [
        ModelRequest(parts=[UserPromptPart(content=_rendered(KAI, "kai's words"))]),
        ModelResponse(parts=[TextPart(content=f"hi <@{KAI}> and <@{NIA}>")]),
        ModelRequest(parts=[UserPromptPart(content=_rendered(NIA, "nia's words"))]),
    ]

    await memory.write_history(7, history)
    stored = (await memory._redis.get(memory._history_key(7))).decode()

    assert "kai's words" not in stored and KAI not in stored
    assert BLOCKED_PLACEHOLDER in stored
    assert "nia's words" in stored and NIA in stored  # control
    # The live list the engine keeps using is not changed.
    assert "kai's words" in history[0].parts[0].content


def test_a_history_kept_before_the_opt_out_is_blanked_for_the_model(kai_opted_out):
    raw = ModelMessagesTypeAdapter.dump_json(
        [ModelRequest(parts=[UserPromptPart(content=_rendered(KAI, "kai's words"))])]
    )
    history = list(ModelMessagesTypeAdapter.validate_json(raw))

    assert blank_model_messages(history)[0].parts[0].content == BLOCKED_PLACEHOLDER
    get_blocked_users().load(2, [])
    assert "kai's words" in blank_model_messages(history)[0].parts[0].content  # control


async def test_topic_and_notes_never_store_an_opted_out_id(kai_opted_out):
    memory = ChatMemory(fakeredis.aioredis.FakeRedis())

    await memory.write_topic(7, f"kai ({KAI}) and nia ({NIA}) on shaders")
    await memory.write_notes(7, f"<@{KAI}> asked, <@{NIA}> answered")

    topic = await memory.topic_for_activation(7)
    notes = await memory.get_notes(7)
    assert KAI not in topic and KAI not in notes
    assert NIA in topic and NIA in notes  # control


# -- the guild memory, as a prompt sees it ------------------------------------------


async def test_guild_memory_loads_without_lines_or_notes_about_opted_out_people(
    kai_opted_out, monkeypatch
):
    monkeypatch.setattr(
        "smarter_dev.bot.services.guild_chat_memory_service.chat_memory_enabled",
        lambda: True,
    )
    now = datetime(2026, 10, 7, tzinfo=UTC).isoformat()
    body = {
        "memory_enabled": True,
        "content": f"# People\n- kai (id {KAI}) loves shaders\n- nia (id {NIA}) runs the jam",
        "behavior": f"Don't tease kai ({KAI}).\nKeep it short.",
        "personality": "Dry.",
        "updated_at": now,
        "notes": [
            {"content": f"kai (id {KAI}) was quiet", "created_at": now},
            {"content": f"nia (id {NIA}) shipped", "created_at": now},
        ],
    }
    api = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(json=lambda: body)))

    snapshot = await GuildChatMemoryService(api).load_snapshot("99")

    assert snapshot.long_term_memory == f"# People\n- nia (id {NIA}) runs the jam"
    assert snapshot.behavior == "Keep it short."
    assert snapshot.personality == "Dry."  # control: nothing about anyone
    assert [n.text for n in snapshot.notes] == [f"nia (id {NIA}) shipped"]


# -- proactive history ----------------------------------------------------------------


def _line(author: str, text: str, message_id: str = "5") -> str:
    return f"[2026-10-07 22:57Z] [id={message_id}] A·u{author[:2]} (uid={author}): {text}"


async def test_proactive_history_is_written_with_the_opted_out_member_blanked(
    kai_opted_out,
):
    from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore

    store = ProactiveHistoryStore(fakeredis.aioredis.FakeRedis())
    transcript = "\n".join(
        [
            _line(KAI, "kai's first line"),
            "kai's second line",
            _line(NIA, f"nia answers <@{KAI}>", "6"),
            "nia's second line",
        ]
    )
    history = [ModelRequest(parts=[UserPromptPart(content=transcript)])]

    await store.write_guild(42, history)
    stored = (await store.read_guild(42))[0].parts[0].content

    assert stored == "\n".join(
        [
            BLOCKED_PLACEHOLDER,
            _line(NIA, "nia answers @[blocked user]", "6"),
            "nia's second line",
        ]
    )
    # Control: nobody on the list, the history is stored as it was.
    get_blocked_users().load(2, [])
    await store.write_guild(42, history)
    assert (await store.read_guild(42))[0].parts[0].content == transcript


async def test_a_purge_rewrite_is_stored_as_the_purge_checked_it(kai_opted_out):
    from smarter_dev.bot.proactive.history_store import ProactiveHistoryStore

    store = ProactiveHistoryStore(fakeredis.aioredis.FakeRedis())
    history = [ModelRequest(parts=[UserPromptPart(content=_line(KAI, "kept"))])]

    await store.write_guild(42, history, keep_clock=True)

    assert (await store.read_guild(42))[0].parts[0].content == _line(KAI, "kept")


async def test_idle_compaction_never_shows_the_summariser_an_opted_out_member(
    monkeypatch,
):
    from smarter_dev.bot.plugins import proactive
    from tests.bot.proactive_idle_compaction_test import GUILD
    from tests.bot.proactive_idle_compaction_test import IDLE
    from tests.bot.proactive_idle_compaction_test import Setup

    for blocked, seen in (([], True), ([KAI], False)):
        get_blocked_users().load(0, [])
        setup = Setup(monkeypatch)
        # Kept while nobody had opted out.
        await setup.store.write_guild(
            GUILD,
            [
                ModelRequest(parts=[UserPromptPart(content=_line(KAI, "my cat is sick"))]),
                ModelResponse(parts=[TextPart(content="see a vet")]),
            ],
        )
        await setup.backdate(IDLE + 1)
        get_blocked_users().load(1, blocked)

        assert await proactive.compact_idle_histories(setup.runtime) == {GUILD: "folded"}
        assert ("my cat is sick" in str(setup.summaries[0])) is seen


async def test_idle_compaction_waits_for_the_list_to_load(monkeypatch):
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
    from tests.bot.proactive_idle_compaction_test import GUILD
    from tests.bot.proactive_idle_compaction_test import IDLE
    from tests.bot.proactive_idle_compaction_test import Setup

    get_blocked_users().load(0, [])  # what the store and the fold read
    cold = BlockedUsersCache()  # what the sweep asks: never loaded
    monkeypatch.setattr("smarter_dev.bot.plugins.proactive.get_blocked_users", lambda: cold)
    setup = Setup(monkeypatch)
    await setup.store.write_guild(
        GUILD,
        [
            ModelRequest(parts=[UserPromptPart(content=_line(NIA, "my cat is sick"))]),
            ModelResponse(parts=[TextPart(content="see a vet")]),
        ],
    )
    await setup.backdate(IDLE + 1)

    assert await proactive.compact_idle_histories(setup.runtime) == {}
    assert setup.summaries == []

    cold.load(1, [])
    assert await proactive.compact_idle_histories(setup.runtime) == {GUILD: "folded"}
    assert "my cat is sick" in str(setup.summaries[0])
