"""The AI assistant opt-out behind ``/privacy`` (#92), bot side.

The control acts only for the person who clicks, an opted-out person who
engages the bot gets a notice and no model call, and someone who opted back
in stays hidden for every message they wrote before it. Synthetic users
only: kai (opts out and back in) and nia.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import hikari
import pytest

from smarter_dev.bot.agents import chat_context
from smarter_dev.bot.agents.chat_input_format import build_agent_call
from smarter_dev.bot.plugins import mention
from smarter_dev.bot.plugins import privacy_notice as privacy_plugin
from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.privacy import opt_out
from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import first_snowflake
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.services.privacy_service import OptOut
from smarter_dev.bot.services.privacy_service import PrivacyApiService
from smarter_dev.bot.services.privacy_service import parse_blocked_users
from smarter_dev.shared.privacy_purge import BLOCKED_PLACEHOLDER

KAI = 111111111111111111
NIA = 222222222222222222
BOT = 999
OPTED_IN = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


def _id_at(moment: datetime) -> int:
    return first_snowflake(moment) + 1


OLD = _id_at(OPTED_IN - timedelta(hours=1))
NEW = _id_at(OPTED_IN + timedelta(minutes=1))


# -- the opt-in cutoff ---------------------------------------------------------


def test_a_person_who_opted_back_in_is_hidden_only_before_the_time():
    cache = BlockedUsersCache()
    cache.load(4, [], read_from={KAI: OPTED_IN})

    assert not cache.is_blocked(KAI)  # live events are always newer
    assert cache.is_blocked(KAI, OLD)
    assert not cache.is_blocked(KAI, NEW)
    assert cache.is_blocked(KAI, "")  # no usable id: hidden
    assert not cache.is_blocked(NIA, OLD)


def test_the_list_response_carries_the_opt_in_times():
    snapshot = parse_blocked_users(
        {"revision": 2, "user_ids": [], "read_from": {str(KAI): OPTED_IN.isoformat()}}
    )
    assert snapshot.read_from == {str(KAI): first_snowflake(OPTED_IN)}
    # A server without the field still parses.
    assert parse_blocked_users({"revision": 1, "user_ids": []}).read_from == {}


def _message(message_id, author_id, name, content, *, reply_to=None):
    return SimpleNamespace(
        id=message_id,
        created_at=OPTED_IN,
        timestamp=OPTED_IN,
        author=SimpleNamespace(id=author_id, username=name, global_name=name, is_bot=False),
        member=None,
        content=content,
        type=0,
        referenced_message=reply_to,
        user_mentions_ids=(),
        mentions_everyone=False,
        attachments=[],
        stickers=[],
        reactions=[],
    )


@pytest.fixture
def kai_opted_back_in():
    blocked = get_blocked_users()
    blocked.load(3, [], read_from={KAI: OPTED_IN})
    return blocked


def _history():
    old = _message(OLD, KAI, "kai", "written while opted out")
    return [
        old,
        _message(OLD + 1, NIA, "nia", "agreed", reply_to=old),
        _message(NEW, KAI, "kai", "written after opting back in"),
    ]


def test_proactive_input_hides_what_was_written_before_the_opt_in(kai_opted_back_in):
    old, reply, new = (proactive.channel_message_from_hikari(m) for m in _history())

    assert old.blocked and old.content == "" and old.author_id == ""
    assert reply.reply_to_id is None and reply.replies_to_blocked
    assert not new.blocked and new.content == "written after opting back in"


async def test_chat_input_hides_what_was_written_before_the_opt_in(
    kai_opted_back_in, monkeypatch
):
    from tests.bot.privacy_model_input_test import _fake_bot
    from tests.bot.privacy_model_input_test import _Memory

    monkeypatch.setattr(
        chat_context, "fetch_channel_info", AsyncMock(return_value={"channel_name": "general"})
    )
    messages = _history()
    agent_input = await chat_context.build_followup_input(
        bot=_fake_bot(messages), channel_id=1, guild_id=2, queued=messages, memory=_Memory()
    )
    prompt, history = build_agent_call(agent_input, [])
    rendered = prompt + str(history)

    assert "written while opted out" not in rendered and str(OLD) not in rendered
    assert BLOCKED_PLACEHOLDER in rendered
    assert "written after opting back in" in rendered


# -- the control ----------------------------------------------------------------


def _click(custom_id: str, user_id: int):
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.custom_id = custom_id
    interaction.user = SimpleNamespace(id=user_id)
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()
    return SimpleNamespace(interaction=interaction)


def _service(state: OptOut | Exception = OptOut(False, None)):
    service = SimpleNamespace()
    if isinstance(state, Exception):
        service.get_opt_out = AsyncMock(side_effect=state)
        service.set_opt_out = AsyncMock(side_effect=state)
    else:
        service.get_opt_out = AsyncMock(return_value=state)
        service.set_opt_out = AsyncMock(return_value=state)
    return service


def _buttons(components):
    return [(b.custom_id, b.label) for row in components for b in row.components]


@pytest.mark.parametrize("action", [opt_out.OPEN, opt_out.SET, opt_out.CLEAR])
async def test_a_click_by_anyone_but_the_person_shown_changes_nothing(action):
    service = _service()
    event = _click(opt_out.custom_id(action, KAI), NIA)

    assert await opt_out.handle_interaction(event, service)

    service.get_opt_out.assert_not_awaited()
    service.set_opt_out.assert_not_awaited()
    response = event.interaction.create_initial_response
    response.assert_awaited_once()
    assert response.await_args.args[1] == opt_out.NOT_YOURS
    assert response.await_args.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL


async def test_open_shows_the_state_with_one_button_to_change_it():
    service = _service(OptOut(False, None))
    event = _click(opt_out.custom_id(opt_out.OPEN, KAI), KAI)

    await opt_out.handle_interaction(event, service)

    service.get_opt_out.assert_awaited_once_with(str(KAI))
    first = event.interaction.create_initial_response.await_args
    assert first.args[0] == hikari.ResponseType.DEFERRED_MESSAGE_CREATE
    assert first.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    edit = event.interaction.edit_initial_response.await_args
    assert opt_out.NOT_OPTED_OUT in edit.args[0] and opt_out.EXPLANATION in edit.args[0]
    assert _buttons(edit.kwargs["components"]) == [(f"ai_opt_out:set:{KAI}", "Opt out")]


@pytest.mark.parametrize(
    ("action", "opted_out", "state", "button"),
    [
        (opt_out.SET, True, OptOut(True, "opt_out"), (f"ai_opt_out:clear:{KAI}", "Opt back in")),
        (opt_out.CLEAR, False, OptOut(False, None), (f"ai_opt_out:set:{KAI}", "Opt out")),
    ],
    ids=["opt-out", "opt-back-in"],
)
async def test_set_and_clear_change_the_clicking_persons_state(
    action, opted_out, state, button
):
    service = _service(state)
    event = _click(opt_out.custom_id(action, KAI), KAI)

    await opt_out.handle_interaction(event, service)

    service.set_opt_out.assert_awaited_once_with(str(KAI), opted_out=opted_out)
    assert (
        event.interaction.create_initial_response.await_args.args[0]
        == hikari.ResponseType.DEFERRED_MESSAGE_UPDATE
    )
    edit = event.interaction.edit_initial_response.await_args
    assert _buttons(edit.kwargs["components"]) == [button]


async def test_an_opt_out_from_a_deletion_says_so_and_offers_no_button():
    service = _service(OptOut(True, "purge"))
    event = _click(opt_out.custom_id(opt_out.CLEAR, KAI), KAI)

    await opt_out.handle_interaction(event, service)

    edit = event.interaction.edit_initial_response.await_args
    assert opt_out.OPTED_OUT_BY_DELETION in edit.args[0]
    assert edit.kwargs["components"] == []


async def test_a_failed_call_answers_with_a_sentence_and_no_id(caplog):
    service = _service(RuntimeError(f"boom {KAI}"))
    event = _click(opt_out.custom_id(opt_out.SET, KAI), KAI)

    await opt_out.handle_interaction(event, service)

    edit = event.interaction.edit_initial_response.await_args
    assert edit.args[0] == opt_out.UNAVAILABLE and edit.kwargs["components"] == []
    assert str(KAI) not in caplog.text


async def test_other_components_are_left_alone():
    event = _click("share_balance", KAI)
    assert not await opt_out.handle_interaction(event, _service())
    event.interaction.create_initial_response.assert_not_awaited()


async def test_privacy_adds_the_button_for_the_caller():
    ctx = SimpleNamespace(respond=AsyncMock(), author=SimpleNamespace(id=KAI))

    await privacy_plugin.privacy.callback(ctx)

    kwargs = ctx.respond.await_args.kwargs
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert _buttons(kwargs["components"]) == [
        (f"ai_opt_out:open:{KAI}", opt_out.OPEN_LABEL)
    ]


async def test_the_service_sends_the_id_in_the_body():
    response = SimpleNamespace(json=lambda: {"opted_out": True, "source": "opt_out", "revision": 1})
    api = SimpleNamespace(post=AsyncMock(return_value=response), put=AsyncMock(return_value=response))
    service = PrivacyApiService(api)

    assert await service.get_opt_out(str(KAI)) == OptOut(True, "opt_out")
    assert await service.set_opt_out(str(KAI), opted_out=True) == OptOut(True, "opt_out")

    api.post.assert_awaited_once_with(
        "/privacy/opt-out/state", json_data={"discord_user_id": str(KAI)}
    )
    api.put.assert_awaited_once_with(
        "/privacy/opt-out", json_data={"discord_user_id": str(KAI), "opted_out": True}
    )


# -- the notice -------------------------------------------------------------------


@pytest.fixture
def notice_bot(monkeypatch):
    monkeypatch.setattr(mention, "_opted_out_notice_at", {})
    registry_getter = MagicMock()
    monkeypatch.setattr(mention, "get_chat_engine_registry", registry_getter)
    bot = SimpleNamespace(
        get_me=MagicMock(return_value=SimpleNamespace(id=BOT)),
        rest=SimpleNamespace(create_message=AsyncMock()),
    )
    monkeypatch.setattr(mention, "plugin", SimpleNamespace(bot=bot))
    bot.registry_getter = registry_getter
    return bot


def _engaging(author_id, *, mentions=(BOT,)):
    message = SimpleNamespace(
        id=NEW,
        author=SimpleNamespace(id=author_id, is_bot=False),
        user_mentions_ids=list(mentions),
        referenced_message=None,
    )
    return SimpleNamespace(message=message, guild_id=2, channel_id=1, content="hi")


async def test_an_opted_out_person_engaging_the_bot_gets_one_notice(notice_bot):
    get_blocked_users().load(1, [str(KAI)])

    await mention.on_message_create(_engaging(KAI))
    await mention.on_message_create(_engaging(KAI))

    notice_bot.registry_getter.assert_not_called()
    notice_bot.rest.create_message.assert_awaited_once()
    call = notice_bot.rest.create_message.await_args
    assert call.args == (1, mention.OPTED_OUT_NOTICE)
    assert call.kwargs["user_mentions"] is False
    assert call.kwargs["mentions_reply"] is False


async def test_no_notice_without_engagement_or_before_the_list_loads(notice_bot):
    get_blocked_users().load(1, [str(KAI)])
    await mention.on_message_create(_engaging(KAI, mentions=()))

    get_blocked_users().reset()
    await mention.on_message_create(_engaging(KAI))

    notice_bot.rest.create_message.assert_not_awaited()
    notice_bot.registry_getter.assert_not_called()
