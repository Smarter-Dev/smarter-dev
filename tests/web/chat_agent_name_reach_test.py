"""The public chat-agent page's wider name check, and the admin fix (#105).

A bare name the agent picked up in conversation (a first name, a display
name) is in no tag, so the check only knew it if a tag somewhere used it. The
page now also knows every tag in every guild's notes and revisions, the names
the guild's moderation, forum and help records keep, and the Discord username
and global name of every Discord-linked site account. And the admin page can
tag a bare name in the three blocks, with a revision recorded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import UUID

import pytest
from skrift.db.models.oauth_account import OAuthAccount
from sqlalchemy import select

from smarter_dev.shared.member_tags import tag_name
from smarter_dev.web.bot_admin.chat_memory import ChatMemoryAdminController
from smarter_dev.web.chat_agent_page_controller import CHAT_AGENT_PATH
from smarter_dev.web.chat_agent_public import check_public_blocks
from smarter_dev.web.chat_memory_retag import ADMIN_TAG_MODEL_NAME
from smarter_dev.web.chat_memory_retag import tag_request_problem
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision
from smarter_dev.web.models import ForumAgent
from smarter_dev.web.models import ForumAgentResponse
from smarter_dev.web.models import HelpConversation
from smarter_dev.web.models import ModerationAction
from tests.web.chat_agent_public_test import _CHANNEL
from tests.web.chat_agent_public_test import _GUILD
from tests.web.chat_agent_public_test import _OTHER_GUILD
from tests.web.chat_agent_public_test import _admin_request
from tests.web.chat_agent_public_test import _fresh_blocks_cache  # noqa: F401
from tests.web.chat_agent_public_test import _seed_memory
from tests.web.chat_agent_public_test import _switch_on
from tests.web.chat_agent_public_test import client  # noqa: F401
from tests.web.user_content_test import _user

KAI = "111111111111111111"
RIO = "444444444444444444"
_FORUM_AGENT = UUID("00000000-0000-0000-0000-000000000105")
_REPORT_LINE = "Report failures to Rio in #bot-dev."


async def _behavior_check(db_session, behavior: str = _REPORT_LINE):
    memory = await _seed_memory(db_session)
    memory.behavior = behavior
    await db_session.commit()
    return (await check_public_blocks(db_session, memory))["behavior"]


async def _link_discord(db_session, discord_id: str, metadata: dict) -> None:
    user = await _user(db_session)
    db_session.add(
        OAuthAccount(
            provider="discord",
            provider_account_id=discord_id,
            provider_email=None,
            provider_email_verified=False,
            provider_metadata=metadata,
            user_id=user.id,
        )
    )
    await db_session.commit()


# -- the wider reach ----------------------------------------------------------------


async def test_a_bare_name_nothing_knows_still_shows(db_session):
    block = await _behavior_check(db_session)

    assert block.problem is None
    assert block.names_found == ()


async def test_a_linked_accounts_global_name_hides_the_block(db_session):
    await _link_discord(
        db_session, RIO, {"id": RIO, "username": "rio_dev", "global_name": "Rio"}
    )

    block = await _behavior_check(db_session)

    assert block.problem == "it names a member"
    assert block.names_found == ("rio",)
    assert block.shown() == ""


async def test_a_linked_accounts_username_hides_the_block(db_session):
    await _link_discord(db_session, RIO, {"username": "rio", "global_name": None})

    assert (await _behavior_check(db_session)).names_found == ("rio",)


async def test_a_tag_in_another_guilds_revision_or_note_is_known(db_session):
    db_session.add(
        ChatAgentMemoryRevision(
            guild_id=_OTHER_GUILD, content=f"- <{RIO}:Rio> runs CI", revision=1
        )
    )
    await db_session.commit()
    assert (await _behavior_check(db_session)).names_found == ("rio",)


async def test_a_tag_in_a_note_is_known(db_session):
    db_session.add(
        ChatAgentMemoryNote(
            guild_id=_OTHER_GUILD,
            channel_id=_CHANNEL,
            content="<:rio> asked about CI",
        )
    )
    await db_session.commit()

    assert (await _behavior_check(db_session)).names_found == ("rio",)


@pytest.mark.parametrize(
    "record",
    [
        lambda: ModerationAction(
            guild_id=_GUILD,
            target_user_id=RIO,
            target_username="Rio",
            action_type="warn",
            source="manual",
        ),
        lambda: ModerationAction(
            guild_id=_GUILD,
            target_user_id=KAI,
            target_username="kai",
            moderator_user_id=RIO,
            moderator_username="Rio",
            action_type="warn",
            source="manual",
        ),
        lambda: ForumAgentResponse(
            agent_id=_FORUM_AGENT,
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            thread_id="666666666666666666",
            post_title="t",
            post_content="c",
            author_display_name="Rio",
            decision_reason="r",
            confidence_score=0.5,
        ),
        lambda: HelpConversation(
            session_id="s",
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            user_id=RIO,
            user_username="Rio",
            interaction_type="slash_command",
            user_question="q",
            bot_response="a",
        ),
    ],
    ids=["moderation target", "moderator", "forum author", "help user"],
)
async def test_the_guilds_own_records_name_members(db_session, record):
    db_session.add(
        ForumAgent(
            id=_FORUM_AGENT,
            guild_id=_GUILD,
            name="helper",
            system_prompt="p",
            created_by="admin",
        )
    )
    db_session.add(record())
    await db_session.commit()

    assert (await _behavior_check(db_session)).names_found == ("rio",)


async def test_another_guilds_records_and_the_bots_own_account_are_not_members(
    db_session,
):
    db_session.add_all(
        [
            ModerationAction(
                guild_id=_OTHER_GUILD,
                target_user_id=RIO,
                target_username="Rio",
                action_type="warn",
                source="manual",
            ),
            # The bot's own account moderates as "handler" or "ai".
            ModerationAction(
                guild_id=_GUILD,
                target_user_id=KAI,
                target_username="kai",
                moderator_user_id="777777777777777777",
                moderator_username="Rio",
                action_type="timeout",
                source="handler",
            ),
        ]
    )
    await db_session.commit()

    assert (await _behavior_check(db_session)).problem is None


async def test_a_name_that_is_the_placeholder_does_not_hide_every_block(db_session):
    await _link_discord(db_session, RIO, {"username": "member", "global_name": "You"})

    memory = await _seed_memory(db_session)
    blocks = await check_public_blocks(db_session, memory)

    assert blocks["memory"].problem is None
    assert "a member loves shaders" in blocks["memory"].shown()


async def test_the_page_hides_a_block_with_a_linked_display_name(db_session, client):  # noqa: F811
    await _seed_memory(db_session)
    await _switch_on(db_session, behavior=_REPORT_LINE)
    await _link_discord(db_session, RIO, {"username": "rio_dev", "global_name": "Rio"})

    html = (await client.get(CHAT_AGENT_PATH)).text

    assert "Report failures" not in html
    assert "The behavior is not shown right now." in html


# -- tagging a name -------------------------------------------------------------------


def test_tag_name_tags_whole_words_outside_references_only():
    text = f"Ask Rio or rio. <{RIO}:rio> rio (id {RIO}) <:Rio> <@{RIO}> Rios xRio"

    new, count = tag_name(text, "Rio", RIO)

    assert count == 2
    assert new.startswith(f"Ask <{RIO}:Rio> or <{RIO}:Rio>. <{RIO}:rio> rio (id ")
    assert new.endswith(f"<:Rio> <@{RIO}> Rios xRio")
    assert tag_name(new, "Rio", RIO) == (new, 0)


def test_tag_name_without_an_id_is_an_id_less_tag():
    assert tag_name("Report to Rio.", "Rio") == ("Report to <:Rio>.", 1)


@pytest.mark.parametrize(
    ("name", "user_id", "ok"),
    [
        ("Rio", "", True),
        ("Rio", RIO, True),
        ("R", "", False),
        ("x" * 65, "", False),
        ("<Rio>", "", False),
        ("Rio\nx", "", False),
        ("Rio", "12345", False),
        ("Rio", "abc", False),
        ("Rio:x", "", False),
        ("Rio:x", RIO, True),
    ],
)
def test_the_tag_form_is_checked(name, user_id, ok):
    assert (tag_request_problem(name, user_id) is None) is ok


async def _post_tag(db_session, form: dict, *, csrf: bool = True):
    with (
        patch(
            "smarter_dev.web.bot_admin.chat_memory.verify_csrf",
            new=AsyncMock(return_value=csrf),
        ),
        patch("smarter_dev.web.bot_admin.chat_memory.flash_success") as success,
        patch("smarter_dev.web.bot_admin.chat_memory.flash_error") as error,
    ):
        response = await ChatMemoryAdminController.chat_memory_tag_name.fn(
            None, request=_admin_request(form), db_session=db_session, guild_id=_GUILD
        )
    return response, success, error


async def _revisions(db_session) -> list[ChatAgentMemoryRevision]:
    db_session.expire_all()
    return list(
        (
            await db_session.scalars(
                select(ChatAgentMemoryRevision)
                .where(ChatAgentMemoryRevision.guild_id == _GUILD)
                .order_by(ChatAgentMemoryRevision.revision)
            )
        ).all()
    )


async def test_the_admin_tags_a_name_in_all_three_blocks(db_session, client):  # noqa: F811
    await _seed_memory(db_session, content="## People\n- Rio runs CI")
    await _switch_on(
        db_session,
        behavior=_REPORT_LINE,
        personality="Rio taught me patience.",
    )
    await _link_discord(db_session, RIO, {"username": "rio_dev", "global_name": "Rio"})
    before = await get_guild_memory_blob(db_session, _GUILD)
    revision = before.revision
    assert "Report failures" not in (await client.get(CHAT_AGENT_PATH)).text

    response, success, error = await _post_tag(
        db_session, {"name": "Rio", "discord_id": RIO}
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/chat-memory"
    assert success.called and not error.called
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == f"## People\n- <{RIO}:Rio> runs CI"
    assert memory.behavior == f"Report failures to <{RIO}:Rio> in #bot-dev."
    assert memory.personality == f"<{RIO}:Rio> taught me patience."
    assert memory.revision == revision + 1
    last = (await _revisions(db_session))[-1]
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert last.model_name == ADMIN_TAG_MODEL_NAME
    assert last.revision == memory.revision
    assert last.behavior == memory.behavior
    blocks = await check_public_blocks(db_session, memory)
    assert all(block.problem is None for block in blocks.values())
    html = (await client.get(CHAT_AGENT_PATH)).text
    assert "Report failures to a member in #bot-dev." in html


async def test_the_admin_tags_a_name_without_an_id(db_session):
    await _seed_memory(db_session)
    memory = await get_guild_memory_blob(db_session, _GUILD)
    memory.behavior = _REPORT_LINE
    await db_session.commit()

    _, success, _ = await _post_tag(db_session, {"name": "Rio", "discord_id": ""})

    assert success.called
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.behavior == "Report failures to <:Rio> in #bot-dev."


@pytest.mark.parametrize(
    ("form", "csrf"),
    [
        ({"name": "Rio", "discord_id": RIO}, False),
        ({"name": "R", "discord_id": ""}, True),
        ({"name": "Rio", "discord_id": "12"}, True),
        ({"name": "Nobody", "discord_id": ""}, True),
    ],
    ids=["expired session", "short name", "bad id", "name not there"],
)
async def test_a_tag_that_cannot_apply_changes_nothing(db_session, form, csrf):
    await _seed_memory(db_session)
    memory = await get_guild_memory_blob(db_session, _GUILD)
    memory.behavior = _REPORT_LINE
    await db_session.commit()
    revisions = len(await _revisions(db_session))

    _, success, error = await _post_tag(db_session, form, csrf=csrf)

    assert error.called and not success.called
    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).behavior == _REPORT_LINE
    assert len(await _revisions(db_session)) == revisions


async def test_a_tag_that_would_overflow_a_block_is_refused_whole(db_session):
    await _seed_memory(db_session, content="Rio runs CI.")
    memory = await get_guild_memory_blob(db_session, _GUILD)
    filler = "Rio " * ((MAX_BEHAVIOR_CHARS - 10) // 4)
    memory.behavior = filler
    await db_session.commit()

    _, success, error = await _post_tag(db_session, {"name": "Rio", "discord_id": RIO})

    assert error.called and not success.called
    assert "behavior" in error.call_args.args[1]
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == "Rio runs CI."
    assert memory.behavior == filler


async def test_a_tag_needs_a_memory(db_session):
    _, success, error = await _post_tag(db_session, {"name": "Rio", "discord_id": ""})

    assert error.called and not success.called


def test_the_admin_page_has_the_tag_form_and_lists_the_names():
    from tests.web.chat_agent_public_test import REPO

    source = (REPO / "templates/admin/bot/chat_memory/view.html").read_text()

    assert "/chat-memory/tag-name" in source
    assert 'name="name"' in source and 'name="discord_id"' in source
    assert "block.names_found" in source
