"""Moderation triage running the tools the model picks, against fake Discord.

The model is the local OpenAI stand-in from test_moderation_triage_requests;
Discord is a recording fake of the few REST calls the tools make. The database
write and the mod_action dispatch are stubbed out.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hikari
import pytest

from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.agents import moderation_agent
from tests.bot.agents.test_moderation_triage_requests import _EXTRACT
from tests.bot.agents.test_moderation_triage_requests import _FINISH
from tests.bot.agents.test_moderation_triage_requests import openai_stub  # noqa: F401

MEMBER = 42
MODERATOR = 7


def _step(tool: str, args: dict) -> str:
    return (
        f"[[ ## next_thought ## ]]\nUse {tool}.\n\n"
        f"[[ ## next_tool_name ## ]]\n{tool}\n\n"
        f"[[ ## next_tool_args ## ]]\n{json.dumps(args)}\n\n"
        "[[ ## completed ## ]]"
    )


_TIMEOUT = _step("timeout_user", {"user_id": str(MEMBER), "duration": "10m", "reason": "spam"})


class FakeRest:
    def __init__(self):
        self.calls: list[tuple] = []
        self.threads: set[str] = set()
        self.loops: set = set()
        self.block = None  # an Event edit_member waits on, when set

    def _record(self, *call):
        self.calls.append(call)
        self.threads.add(threading.current_thread().name)
        self.loops.add(asyncio.get_running_loop())

    async def fetch_member(self, guild_id, user_id):
        self._record("fetch_member", user_id)
        user = SimpleNamespace(fetch_dm_channel=AsyncMock(return_value="dm"))
        return SimpleNamespace(id=user_id, display_name=f"user{user_id}", username=f"user{user_id}", user=user)

    async def edit_member(self, guild_id, user_id, **kwargs):
        self._record("edit_member", user_id)
        if self.block is not None:
            await self.block.wait()

    async def fetch_message(self, channel_id, message_id):
        self._record("fetch_message", message_id)
        return SimpleNamespace(author=SimpleNamespace(id=MODERATOR))

    async def delete_message(self, channel_id, message_id):
        self._record("delete_message", message_id)

    async def create_message(self, channel, content=None, **kwargs):
        self._record("create_message", channel)

    def actions(self):
        return [c for c in self.calls if c[0] in ("edit_member", "delete_message")]


@pytest.fixture
def discord(monkeypatch):
    rest = FakeRest()
    bot = SimpleNamespace(rest=rest, get_me=lambda: SimpleNamespace(id=1))

    def permissions_for(member):
        if member.id == MODERATOR:
            return hikari.Permissions.MODERATE_MEMBERS
        return hikari.Permissions.NONE

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(mod_tools.lightbulb.utils, "permissions_for", permissions_for)
    monkeypatch.setattr(mod_tools, "get_db_session_context", session)
    monkeypatch.setattr(mod_tools.mod_action_ops, "create_action", AsyncMock())
    monkeypatch.setattr(mod_tools, "dispatch_mod_action", AsyncMock())
    monkeypatch.setattr(mod_tools, "record_guild_event", AsyncMock())
    return bot


_ALL = ("timeout", "purge", "delete")


async def _triage(bot, enabled_tools=_ALL):
    return await moderation_agent.run_moderation_agent(
        bot=bot,
        guild_id="1",
        channel_id="2",
        trigger_message_content="@mods spam here",
        trigger_author="reporter",
        context_messages=[],
        guild_instructions="",
        enabled_tools=None if enabled_tools is None else list(enabled_tools),
        trigger_message_id="3",
    )


async def test_a_chosen_tool_runs_once_on_the_bots_event_loop(openai_stub, discord):  # noqa: F811
    openai_stub("gpt-6-luna", [_TIMEOUT, _FINISH, _EXTRACT])

    assessment, tracker = await _triage(discord)

    assert tracker.failure is None
    assert discord.rest.actions() == [("edit_member", MEMBER)]
    assert [t["user_id"] for t in tracker.timeouts] == [str(MEMBER)]
    # hikari's REST client belongs to the bot's loop: no worker threads.
    assert discord.rest.threads == {threading.current_thread().name}
    assert discord.rest.loops == {asyncio.get_running_loop()}


async def test_moderators_and_their_messages_are_left_alone(openai_stub, discord):  # noqa: F811
    openai_stub("gpt-6-luna", [
        _step("timeout_user", {"user_id": str(MODERATOR), "duration": "10m", "reason": "x"}),
        _step("purge_messages", {"user_id": str(MODERATOR), "count": 5, "reason": "x"}),
        _step("delete_message", {"message_id": "99", "reason": "x"}),
        _FINISH,
        _EXTRACT,
    ])

    _, tracker = await _triage(discord)

    assert discord.rest.actions() == []
    assert not tracker.has_actions


_ACTION_TOOLS = {"timeout": "timeout_user", "purge": "purge_messages", "delete": "delete_message"}
_UTILITY_TOOLS = ("flag_users", "send_mod_message", "get_user_info", "get_user_history")


@pytest.mark.parametrize(
    "enabled",
    [None, [], ["timeout"], ["purge"], ["delete"], ["timeout", "purge"], list(_ALL)],
    ids=lambda e: "None" if e is None else "+".join(e) or "empty",
)
async def test_only_the_guilds_named_action_tools_are_offered(openai_stub, discord, enabled):  # noqa: F811
    server = openai_stub("gpt-6-luna", [_FINISH, _EXTRACT])

    await _triage(discord, enabled_tools=enabled)

    prompt = json.dumps(server.bodies[0]["messages"])
    for name, tool in _ACTION_TOOLS.items():
        assert (tool in prompt) is (name in (enabled or ())), tool
    for tool in _UTILITY_TOOLS:
        assert tool in prompt


@pytest.mark.parametrize(
    ("stored", "cached"),
    [(None, []), ([], []), (["delete"], ["delete"]), (list(_ALL), list(_ALL))],
)
async def test_both_monitor_config_paths_keep_the_stored_tools(monkeypatch, stored, cached):
    from smarter_dev.bot.plugins import mod_monitor

    config = SimpleNamespace(
        guild_id="1", is_active=True, monitored_role_ids=["5"], instructions=None,
        enabled_tools=stored, context_message_limit=25, response_channel_id=None,
    )

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(mod_monitor, "get_db_session_context", session)
    monkeypatch.setattr(mod_monitor.mod_config_ops, "get_all_active_configs", AsyncMock(return_value=[config]))
    monkeypatch.setattr(mod_monitor.mod_config_ops, "get_config", AsyncMock(return_value=config))
    monkeypatch.setattr(mod_monitor, "_guild_configs", {})

    await mod_monitor._load_configs()
    assert mod_monitor._guild_configs["1"]["enabled_tools"] == cached
    mod_monitor._guild_configs.clear()
    await mod_monitor.refresh_config("1")
    assert mod_monitor._guild_configs["1"]["enabled_tools"] == cached


async def test_a_failure_after_an_action_keeps_it_and_does_not_repeat_it(openai_stub, discord):  # noqa: F811
    openai_stub("gpt-6-luna", [_TIMEOUT, None])

    _, tracker = await _triage(discord)

    assert tracker.failure == "InternalServerError"
    assert discord.rest.actions() == [("edit_member", MEMBER)]
    assert [t["user_id"] for t in tracker.timeouts] == [str(MEMBER)]
    embed = mod_tools.build_triage_report_embed(tracker, "failed", "reporter", "2", "3")
    assert f"Timeout: user{MEMBER}" in [f.name for f in embed.fields]


async def test_cancelling_mid_action_stops_without_replaying_it(openai_stub, discord):  # noqa: F811
    server = openai_stub("gpt-6-luna", [_TIMEOUT, _FINISH, _EXTRACT])
    discord.rest.block = asyncio.Event()

    task = asyncio.create_task(_triage(discord))
    for _ in range(500):
        if discord.rest.actions():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert discord.rest.actions() == [("edit_member", MEMBER)]
    assert len(server.bodies) == 1
