"""An opt-out takes effect in the bot at once, not at the next fetch (#100).

The list is fetched every 60 seconds. Before #100 someone who pressed
"Opt out" right after a fetch was still answered for up to a minute. Now the
button blocks them in this process the moment the web app confirms it, a
fetch already in flight cannot undo that, and the refresh loop fetches at
once. The chat engine checks the list again right before the model call.
Synthetic users only: kai and nia.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import hikari

from smarter_dev.bot.agents.chat_context import reblank_newly_blocked
from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_models import ChannelInfo
from smarter_dev.bot.agents.chat_models import FollowupAgentInput
from smarter_dev.bot.agents.chat_models import InitialAgentInput
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import Message
from smarter_dev.bot.privacy import opt_out
from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import BlockedUsersSnapshot
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.privacy.blocked_users import refresh_loop
from smarter_dev.bot.services.privacy_service import OptOut
from smarter_dev.bot.services.privacy_service import _opt_out

KAI = "111111111111111111"
NIA = "222222222222222222"


def _snapshot(revision: int, *user_ids: str) -> BlockedUsersSnapshot:
    return BlockedUsersSnapshot(revision=revision, user_ids=frozenset(user_ids))


# -- the cache ----------------------------------------------------------------


def test_block_now_blocks_before_any_fetch():
    cache = BlockedUsersCache()
    cache.load(4, [])
    assert not cache.is_blocked(KAI)  # control: the loaded list alone

    cache.block_now(KAI, 5)

    assert cache.is_blocked(KAI)
    assert not cache.is_blocked(NIA)


def test_a_fetch_older_than_the_opt_out_cannot_drop_it():
    cache = BlockedUsersCache()
    cache.load(4, [])
    cache.block_now(KAI, 5)

    # A fetch that started before the opt-out committed lands afterwards.
    cache.replace(_snapshot(4))
    assert cache.is_blocked(KAI)

    # The list that carries the change takes over from the local entry.
    cache.replace(_snapshot(5, KAI))
    assert cache.is_blocked(KAI)
    # Opting back in later (a newer list without them) is honoured.
    cache.replace(_snapshot(6))
    assert not cache.is_blocked(KAI)


def test_without_a_revision_the_entry_holds_until_the_list_moves_on():
    cache = BlockedUsersCache()
    cache.load(4, [])
    cache.block_now(KAI)

    cache.replace(_snapshot(4))
    assert cache.is_blocked(KAI)
    cache.replace(_snapshot(5, KAI))
    cache.replace(_snapshot(6))
    assert not cache.is_blocked(KAI)


async def test_a_requested_refresh_fetches_at_once():
    cache = BlockedUsersCache()
    fetch = AsyncMock(return_value=_snapshot(1))
    task = asyncio.create_task(refresh_loop(cache, fetch, None, interval=3600))
    try:
        for _ in range(50):
            if fetch.await_count:
                break
            await asyncio.sleep(0.01)
        assert fetch.await_count == 1

        await asyncio.sleep(0.05)
        assert fetch.await_count == 1  # control: no request, no fetch

        cache.request_refresh()
        for _ in range(50):
            if fetch.await_count == 2:
                break
            await asyncio.sleep(0.01)
        assert fetch.await_count == 2
    finally:
        task.cancel()


# -- the button -----------------------------------------------------------------


def _click(action: str, user_id: str):
    interaction = MagicMock(spec=hikari.ComponentInteraction)
    interaction.custom_id = opt_out.custom_id(action, user_id)
    interaction.user = SimpleNamespace(id=int(user_id))
    interaction.create_initial_response = AsyncMock()
    interaction.edit_initial_response = AsyncMock()
    return SimpleNamespace(interaction=interaction)


def _service(state: OptOut | Exception):
    if isinstance(state, Exception):
        return SimpleNamespace(set_opt_out=AsyncMock(side_effect=state))
    return SimpleNamespace(set_opt_out=AsyncMock(return_value=state))


async def test_pressing_opt_out_blocks_the_person_in_this_process_at_once():
    blocked = get_blocked_users()
    assert not blocked.is_blocked(KAI)

    await opt_out.handle_interaction(
        _click(opt_out.SET, KAI), _service(OptOut(True, "opt_out", revision=1))
    )

    assert blocked.is_blocked(KAI)
    assert not blocked.is_blocked(NIA)
    assert blocked._refresh_requested.is_set()
    # The fetched list from before the change does not undo it.
    blocked.replace(_snapshot(0))
    assert blocked.is_blocked(KAI)


async def test_opting_back_in_or_a_failed_call_blocks_no_one():
    blocked = get_blocked_users()

    await opt_out.handle_interaction(
        _click(opt_out.CLEAR, KAI), _service(OptOut(False, None, revision=1))
    )
    await opt_out.handle_interaction(
        _click(opt_out.SET, NIA), _service(RuntimeError("down"))
    )

    assert not blocked.is_blocked(KAI)
    assert not blocked.is_blocked(NIA)


def test_the_opt_out_response_carries_the_revision():
    assert _opt_out({"opted_out": True, "source": "opt_out", "revision": 7}).revision == 7
    assert _opt_out({"opted_out": True, "source": "opt_out"}).revision is None


# -- the engine's re-check before the model call ----------------------------------


def _message(message_id: str, author_id: str, body: str) -> Message:
    return Message(message_id=message_id, author_id=author_id, body=body)


def _common():
    return {
        "me": Me(user_id="999", username="bot"),
        "authors": [
            Author(user_id=KAI, username="kai", display_name="kai"),
            Author(user_id=NIA, username="nia", display_name="nia"),
        ],
        "channel": ChannelInfo(channel_id="1", name="general"),
        "now_utc": datetime.now(UTC),
    }


def test_recheck_blanks_someone_who_opted_out_while_the_turn_was_built():
    agent_input = FollowupAgentInput(
        new_messages=[_message("10", KAI, "kai's words"), _message("11", NIA, "nia's")],
        **_common(),
    )
    blocked = BlockedUsersCache()
    blocked.load(1, [])
    assert not reblank_newly_blocked(agent_input, blocked)
    assert agent_input.new_messages[0].body == "kai's words"  # control

    blocked.block_now(KAI, 2)
    assert not reblank_newly_blocked(agent_input, blocked)

    kai, nia = agent_input.new_messages
    assert kai.blocked and kai.body == "" and kai.author_id == "" and kai.message_id == ""
    assert nia.body == "nia's"
    assert [a.user_id for a in agent_input.authors] == [NIA]


def test_recheck_stops_a_turn_whose_trigger_opted_out():
    agent_input = InitialAgentInput(
        channel_history=[_message("9", NIA, "earlier")],
        activation_message=_message("10", KAI, "@bot hi"),
        **_common(),
    )
    blocked = BlockedUsersCache()
    blocked.load(1, [])
    assert not reblank_newly_blocked(agent_input, blocked)  # control

    blocked.block_now(KAI, 2)
    assert reblank_newly_blocked(agent_input, blocked)


# -- the same, through a whole engine turn -----------------------------------------


class _OptsOutWhileBuilding:
    """Wraps one of the engine-test harness's input builders: the author opts
    out while the turn is being prepared."""

    def __init__(self, build, user_id: str):
        self._build = build
        self._user_id = user_id

    async def __call__(self, **kwargs):
        built = await self._build(**kwargs)
        get_blocked_users().block_now(self._user_id, 1)
        return built


async def test_an_engine_turn_whose_author_opts_out_mid_turn_never_calls_the_model():
    import fakeredis.aioredis

    from tests.bot.services.test_chat_engine_memory import EMPTY_SNAPSHOT
    from tests.bot.services.test_chat_engine_memory import _chat_agent
    from tests.bot.services.test_chat_engine_memory import _EngineHarness
    from tests.bot.services.test_chat_engine_memory import _make_engine
    from tests.bot.services.test_chat_engine_memory import _memory_service
    from tests.bot.services.test_chat_engine_memory import fake_memory

    redis = fakeredis.aioredis.FakeRedis()
    for opts_out, model_calls in ((False, 1), (True, 0)):
        get_blocked_users().load(0, [])
        engine = _make_engine(redis, _memory_service(EMPTY_SNAPSHOT))
        agent = _chat_agent()
        harness = _EngineHarness(fake_memory=fake_memory.__wrapped__(), agent=agent)
        if opts_out:
            harness._build_initial = _OptsOutWhileBuilding(harness._build_initial, "200")
        with harness:
            await engine._run_once(first_activation=True)
        assert agent.run.await_count == model_calls
        if model_calls:
            # Control turn: the remember tool is told whose words it read.
            assert agent.run.await_args.kwargs["deps"].source_user_ids == {"200"}


async def test_a_history_kept_before_the_opt_out_reaches_the_model_blanked():
    import fakeredis.aioredis
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.messages import UserPromptPart

    from tests.bot.services.test_chat_engine_memory import EMPTY_SNAPSHOT
    from tests.bot.services.test_chat_engine_memory import _chat_agent
    from tests.bot.services.test_chat_engine_memory import _EngineHarness
    from tests.bot.services.test_chat_engine_memory import _make_engine
    from tests.bot.services.test_chat_engine_memory import _memory_service
    from tests.bot.services.test_chat_engine_memory import _queue_a_message
    from tests.bot.services.test_chat_engine_memory import fake_memory

    kept = (
        f'<message id="5" sent-utc="2026-10-07T22:50:00Z" user-id="{KAI}" '
        f'username="kai">\nkai said this earlier\n</message>'
    )
    for blocked, seen in (([], True), ([KAI], False)):
        get_blocked_users().load(1, blocked)
        memory = fake_memory.__wrapped__()
        memory.read_history = AsyncMock(
            return_value=[ModelRequest(parts=[UserPromptPart(content=kept)])]
        )
        engine = _make_engine(fakeredis.aioredis.FakeRedis(), _memory_service(EMPTY_SNAPSHOT))
        _queue_a_message(engine)
        agent = _chat_agent()
        with _EngineHarness(fake_memory=memory, agent=agent):
            await engine._run_once(first_activation=False)
        history = str(agent.run.await_args.kwargs["message_history"])
        assert ("kai said this earlier" in history) is seen


async def test_a_skipped_follow_up_keeps_the_memory_blocks_for_the_next_turn():
    """A follow-up whose every new message's author opted out is skipped. The
    memory blocks it was due to re-send must go out on the next turn."""
    import fakeredis.aioredis

    from tests.bot.services.test_chat_engine_memory import EMPTY_SNAPSHOT
    from tests.bot.services.test_chat_engine_memory import _chat_agent
    from tests.bot.services.test_chat_engine_memory import _EngineHarness
    from tests.bot.services.test_chat_engine_memory import _make_engine
    from tests.bot.services.test_chat_engine_memory import _memory_service
    from tests.bot.services.test_chat_engine_memory import _queue_a_message
    from tests.bot.services.test_chat_engine_memory import fake_memory

    for opts_out, model_calls, reemit_after in ((False, 1, False), (True, 0, True)):
        get_blocked_users().load(0, [])
        engine = _make_engine(fakeredis.aioredis.FakeRedis(), _memory_service(EMPTY_SNAPSHOT))
        engine._reemit_long_term_memory = True
        _queue_a_message(engine)
        agent = _chat_agent()
        harness = _EngineHarness(fake_memory=fake_memory.__wrapped__(), agent=agent)
        if opts_out:
            harness._build_followup = _OptsOutWhileBuilding(harness._build_followup, "200")
        with harness:
            await engine._run_once(first_activation=False)
        assert agent.run.await_count == model_calls
        assert engine._reemit_long_term_memory is reemit_after
