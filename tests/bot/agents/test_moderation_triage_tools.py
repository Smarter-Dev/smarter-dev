"""Moderation triage running the tools the model picks, against fake Discord.

The model is the local OpenAI stand-in from test_moderation_triage_requests;
Discord is a recording fake of the REST calls and cache lookups the tools
make, including the real staff check (``target_refusal``). The database
write, the mod_action dispatch and the event log are stubbed out.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hikari
import pytest

from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.agents import moderation_agent
from tests.bot.agents.test_moderation_triage_requests import _EXTRACT
from tests.bot.agents.test_moderation_triage_requests import _FINISH
from tests.bot.agents.test_moderation_triage_requests import openai_stub  # noqa: F401

GUILD = 1  # also @everyone's role id
BOT = 2
OWNER = 99
MEMBER = 42
MODERATOR = 7
MOD_ROLE = 500
MESSAGE_BY_MEMBER = 900
MESSAGE_BY_MODERATOR = 901

_STAFF = [
    hikari.Permissions.ADMINISTRATOR,
    hikari.Permissions.MANAGE_GUILD,
    hikari.Permissions.MANAGE_ROLES,
    hikari.Permissions.MANAGE_CHANNELS,
    hikari.Permissions.MANAGE_MESSAGES,
    hikari.Permissions.MODERATE_MEMBERS,
    hikari.Permissions.KICK_MEMBERS,
    hikari.Permissions.BAN_MEMBERS,
    hikari.Permissions.MANAGE_NICKNAMES,
    hikari.Permissions.MANAGE_WEBHOOKS,
    hikari.Permissions.VIEW_AUDIT_LOG,
]


def _role(role_id, permissions=hikari.Permissions.NONE):
    return SimpleNamespace(id=role_id, permissions=permissions)


def _step(tool: str, args: dict) -> str:
    return (
        f"[[ ## next_thought ## ]]\nUse {tool}.\n\n"
        f"[[ ## next_tool_name ## ]]\n{tool}\n\n"
        f"[[ ## next_tool_args ## ]]\n{json.dumps(args)}\n\n"
        "[[ ## completed ## ]]"
    )


_TIMEOUT = _step("timeout_user", {"user_id": str(MEMBER), "duration": "10m", "reason": "spam"})


class FakeDiscord:
    """REST and cache for one guild. ``fail`` maps a REST method to what it raises."""

    def __init__(self):
        self.roles = {GUILD: _role(GUILD), MOD_ROLE: _role(MOD_ROLE, hikari.Permissions.MODERATE_MEMBERS)}
        self.member_roles = {MEMBER: [], MODERATOR: [MOD_ROLE], OWNER: [], BOT: []}
        self.cached_roles: set[int] = set(self.roles)
        self.guild_cached = True
        self.fail: dict[str, BaseException] = {}
        self.block: asyncio.Event | None = None  # destructive calls wait on it when set
        self.calls: list[tuple] = []
        self.threads: set[str] = set()
        self.loops: set = set()
        self.rest = self
        self.cache = SimpleNamespace(get_guild=self._cached_guild, get_role=self._cached_role)

    def get_me(self):
        return SimpleNamespace(id=BOT)

    def _cached_guild(self, guild_id):
        return SimpleNamespace(owner_id=OWNER) if self.guild_cached else None

    def _cached_role(self, role_id):
        return self.roles.get(role_id) if role_id in self.cached_roles else None

    async def _call(self, name, *args):
        self.calls.append((name, *args))
        self.threads.add(threading.current_thread().name)
        self.loops.add(asyncio.get_running_loop())
        if name in self.fail:
            raise self.fail[name]

    async def _destructive(self, name, *args):
        await self._call(name, *args)
        if self.block is not None:
            await self.block.wait()

    async def fetch_member(self, guild_id, user_id):
        await self._call("fetch_member", user_id)
        user = SimpleNamespace(fetch_dm_channel=AsyncMock(return_value="dm"))
        return SimpleNamespace(
            id=user_id, role_ids=self.member_roles.get(user_id, []),
            display_name=f"user{user_id}", username=f"user{user_id}", user=user,
        )

    async def fetch_guild(self, guild_id):
        await self._call("fetch_guild")
        return SimpleNamespace(owner_id=OWNER)

    async def fetch_roles(self, guild_id):
        await self._call("fetch_roles")
        return list(self.roles.values())

    async def fetch_message(self, channel_id, message_id):
        await self._call("fetch_message", message_id)
        author = MODERATOR if message_id == MESSAGE_BY_MODERATOR else MEMBER
        return SimpleNamespace(author=SimpleNamespace(id=author))

    def fetch_messages(self, channel_id):
        recent = int(hikari.Snowflake.from_datetime(datetime.now(UTC)))

        class _History:
            def limit(self, n):
                return self

            async def __aiter__(self):
                for i in range(3):  # three recent messages by MEMBER
                    yield SimpleNamespace(id=recent + i, author=SimpleNamespace(id=MEMBER))

        return _History()

    async def edit_member(self, guild_id, user_id, **kwargs):
        await self._destructive("edit_member", user_id)

    async def delete_message(self, channel_id, message_id):
        await self._destructive("delete_message", message_id)

    async def delete_messages(self, channel_id, message_ids):
        await self._destructive("delete_messages", tuple(message_ids))

    async def create_message(self, channel, content=None, **kwargs):
        await self._call("create_message", channel)

    def destructive(self):
        return [c for c in self.calls if c[0] in ("edit_member", "delete_message", "delete_messages")]


@pytest.fixture
def discord(monkeypatch):
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(mod_tools, "get_db_session_context", session)
    monkeypatch.setattr(mod_tools.mod_action_ops, "create_action", AsyncMock())
    monkeypatch.setattr(mod_tools, "dispatch_mod_action", AsyncMock())
    monkeypatch.setattr(mod_tools, "record_guild_event", AsyncMock())
    return FakeDiscord()


_ALL = ("timeout", "purge", "delete")


def _tools(discord, enabled=_ALL):
    tools, tracker = mod_tools.create_moderation_tools(
        discord, guild_id=str(GUILD), channel_id="3", trigger_message_id=None, enabled_tools=list(enabled)
    )
    return {t.__name__: t for t in tools}, tracker


async def _triage(bot, enabled_tools=_ALL):
    return await moderation_agent.run_moderation_agent(
        bot=bot,
        guild_id=str(GUILD),
        channel_id="3",
        trigger_message_content="@mods spam here",
        trigger_author="reporter",
        context_messages=[],
        guild_instructions="",
        enabled_tools=None if enabled_tools is None else list(enabled_tools),
        trigger_message_id="4",
    )


async def _cancel_once_destructive(discord, coro):
    discord.block = asyncio.Event()
    task = asyncio.create_task(coro)
    for _ in range(500):
        if discord.destructive():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── The real triage path ─────────────────────────────────────────────


async def test_a_chosen_tool_runs_once_on_the_bots_event_loop(openai_stub, discord):  # noqa: F811
    openai_stub("gpt-6-luna", [_TIMEOUT, _FINISH, _EXTRACT])

    _, tracker = await _triage(discord)

    assert tracker.failure is None
    assert discord.destructive() == [("edit_member", MEMBER)]
    assert tracker.timeouts == [
        {"user_id": str(MEMBER), "username": f"user{MEMBER}", "duration": "10m", "reason": "spam"}
    ]
    # hikari's REST client belongs to the bot's loop: no worker threads.
    assert discord.threads == {threading.current_thread().name}
    assert discord.loops == {asyncio.get_running_loop()}


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
    assert discord.destructive() == [("edit_member", MEMBER)]
    embed = mod_tools.build_triage_report_embed(tracker, "failed", "reporter", "3", "4")
    [confirmed] = [f for f in embed.fields if f.name == "Actions Taken (confirmed)"]
    assert f"user{MEMBER}" in confirmed.value


async def test_cancelling_triage_mid_action_propagates_without_replay(openai_stub, discord, monkeypatch):  # noqa: F811
    server = openai_stub("gpt-6-luna", [_TIMEOUT, _FINISH, _EXTRACT])
    created = []
    real = moderation_agent.create_moderation_tools

    def spy(**kwargs):
        tools, tracker = real(**kwargs)
        created.append(tracker)
        return tools, tracker

    monkeypatch.setattr(moderation_agent, "create_moderation_tools", spy)
    await _cancel_once_destructive(discord, _triage(discord))

    assert discord.destructive() == [("edit_member", MEMBER)]
    assert len(server.bodies) == 1
    assert created[0].timeouts[0]["possibly_applied"] is True


# ── Staff protection: the real check, cache hits and misses ─────────


@pytest.mark.parametrize("permission", _STAFF, ids=lambda p: p.name)
async def test_every_staff_permission_protects_its_holder(discord, permission):
    discord.roles[MOD_ROLE] = _role(MOD_ROLE, permission)
    assert await mod_tools.target_refusal(discord, str(GUILD), str(MODERATOR)) is not None


def test_the_staff_set_is_exactly_the_listed_permissions():
    expected = hikari.Permissions.NONE
    for permission in _STAFF:
        expected |= permission
    assert mod_tools.STAFF_PERMISSIONS == expected


async def test_staff_permission_on_everyone_protects_everybody(discord):
    discord.roles[GUILD] = _role(GUILD, hikari.Permissions.VIEW_AUDIT_LOG)
    assert await mod_tools.target_refusal(discord, str(GUILD), str(MEMBER)) is not None


async def test_a_role_cache_miss_still_finds_the_moderator(discord):
    discord.cached_roles = set()
    discord.guild_cached = False

    refusal = await mod_tools.target_refusal(discord, str(GUILD), str(MODERATOR))

    assert refusal == "Cannot moderate users with staff permissions."
    assert ("fetch_roles",) in discord.calls and ("fetch_guild",) in discord.calls


@pytest.mark.parametrize("case", ["unknown role", "fetch_roles fails", "fetch_member fails"])
async def test_anything_unresolved_refuses(discord, case):
    discord.cached_roles = set()
    if case == "unknown role":
        discord.member_roles[MEMBER] = [777]
    elif case == "fetch_roles fails":
        discord.fail["fetch_roles"] = hikari.HTTPError("down")
    else:
        discord.fail["fetch_member"] = RuntimeError("down")
    assert await mod_tools.target_refusal(discord, str(GUILD), str(MEMBER)) is not None


@pytest.mark.parametrize("user", [OWNER, BOT])
async def test_the_owner_and_the_bot_are_refused(discord, user):
    assert await mod_tools.target_refusal(discord, str(GUILD), str(user)) is not None


async def test_a_plain_member_resolves_from_the_cache(discord):
    assert await mod_tools.target_refusal(discord, str(GUILD), str(MEMBER)) is None
    assert ("fetch_roles",) not in discord.calls


async def test_moderators_and_their_messages_are_left_alone(discord):
    tools, tracker = _tools(discord)
    discord.cached_roles = set()  # through REST, not the cache

    for result in (
        await tools["timeout_user"](str(MODERATOR), "10m", "x"),
        await tools["purge_messages"](str(MODERATOR), 5, "x"),
        await tools["delete_message"](str(MESSAGE_BY_MODERATOR), "x"),
    ):
        assert result["success"] is False
    assert discord.destructive() == []
    assert not tracker.has_actions


# ── The action cap: reserved slots ───────────────────────────────────


async def test_refusals_before_sending_take_no_slot(discord):
    tools, tracker = _tools(discord)

    assert (await tools["timeout_user"](str(MODERATOR), "10m", "x"))["success"] is False
    assert (await tools["timeout_user"](str(MEMBER), "-5m", "x"))["success"] is False
    assert (await tools["timeout_user"](str(MEMBER), "2h", "x"))["success"] is False
    for _ in range(3):
        assert (await tools["delete_message"](str(MESSAGE_BY_MEMBER), "x"))["success"] is True
    assert (await tools["delete_message"](str(MESSAGE_BY_MEMBER), "x"))["success"] is False

    assert len(discord.destructive()) == 3
    assert len(tracker.deletions) == 3


async def test_a_negative_duration_is_refused(discord):
    tools, _ = _tools(discord)
    result = await tools["timeout_user"](str(MEMBER), "-5m", "x")
    assert result["success"] is False
    assert discord.destructive() == []


async def test_failed_recording_still_counts_and_reports_each_action(discord, monkeypatch):
    monkeypatch.setattr(mod_tools.mod_action_ops, "create_action", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(mod_tools, "record_guild_event", AsyncMock(side_effect=RuntimeError("log down")))
    tools, tracker = _tools(discord)

    results = [
        await tools["timeout_user"](str(MEMBER), "10m", "x"),
        await tools["purge_messages"](str(MEMBER), 5, "x"),
        await tools["delete_message"](str(MESSAGE_BY_MEMBER), "x"),
        await tools["timeout_user"](str(MEMBER), "10m", "x"),
        await tools["delete_message"](str(MESSAGE_BY_MEMBER), "x"),
    ]

    assert [r["success"] for r in results] == [True, True, True, False, False]
    assert len(discord.destructive()) == 3
    assert (len(tracker.timeouts), len(tracker.purges), len(tracker.deletions)) == (1, 1, 1)
    assert not any(e.get("possibly_applied") for e in tracker.timeouts + tracker.purges + tracker.deletions)


def _errors():
    return {
        "NotFoundError": hikari.NotFoundError("u", {}, b"", "gone"),
        "ForbiddenError": hikari.ForbiddenError("u", {}, b"", "no"),
        "InternalServerError": hikari.InternalServerError("u", 500, {}, b"", "boom"),
        "HTTPError": hikari.HTTPError("connection lost"),
        "RateLimitTooLongError": hikari.RateLimitTooLongError(
            route="r", is_global=False, retry_after=90, max_retry_after=60, reset_at=0, limit=1, period=1
        ),
        "TimeoutError": TimeoutError(),
    }


@pytest.mark.parametrize("error", list(_errors()))
@pytest.mark.parametrize(
    ("tool", "method", "args", "kind"),
    [
        ("timeout_user", "edit_member", (str(MEMBER), "10m", "x"), "timeouts"),
        ("delete_message", "delete_message", (str(MESSAGE_BY_MEMBER), "x"), "deletions"),
    ],
)
async def test_any_error_from_a_destructive_call_keeps_its_slot(discord, error, tool, method, args, kind):
    # hikari may already have applied it on an earlier, retried attempt.
    discord.fail[method] = _errors()[error]
    tools, tracker = _tools(discord)

    for _ in range(3):
        result = await tools[tool](*args)
        assert result["success"] is False
        assert "Outcome unknown" in result["error"]
    assert (await tools[tool](*args))["error"].startswith("Action limit reached")

    assert len(discord.destructive()) == 3  # once each, never retried
    assert len(getattr(tracker, kind)) == 3
    assert all(e["possibly_applied"] for e in getattr(tracker, kind))


@pytest.mark.parametrize(
    ("deleted", "cause"),
    [
        (2, hikari.BadRequestError("u", {}, b"", "bad")),
        (2, hikari.InternalServerError("u", 500, {}, b"", "boom")),
        (0, hikari.BadRequestError("u", {}, b"", "bad")),
        (0, TimeoutError()),
    ],
)
async def test_a_bulk_delete_stopping_partway_counts_what_went_and_marks_the_rest(discord, deleted, cause):
    error = hikari.BulkDeleteError([SimpleNamespace(id=i) for i in range(deleted)])
    error.__cause__ = cause
    discord.fail["delete_messages"] = error
    tools, tracker = _tools(discord)

    result = await tools["purge_messages"](str(MEMBER), 5, "x")

    assert result["success"] is False
    confirmed = [p["count"] for p in tracker.purges if not p.get("possibly_applied")]
    unknown = [p["count"] for p in tracker.purges if p.get("possibly_applied")]
    assert confirmed == ([deleted] if deleted else [])
    assert unknown == [3 - deleted]


@pytest.mark.parametrize(
    ("tool", "args", "kind"),
    [
        ("timeout_user", (str(MEMBER), "10m", "x"), "timeouts"),
        ("purge_messages", (str(MEMBER), 5, "x"), "purges"),
        ("delete_message", (str(MESSAGE_BY_MEMBER), "x"), "deletions"),
    ],
)
async def test_cancelling_a_destructive_call_tracks_it_as_possibly_applied(discord, tool, args, kind):
    tools, tracker = _tools(discord)

    await _cancel_once_destructive(discord, tools[tool](*args))

    [entry] = getattr(tracker, kind)
    assert entry["possibly_applied"] is True
    assert len(discord.destructive()) == 1


# ── The report ───────────────────────────────────────────────────────


def test_the_report_keeps_confirmed_unknown_and_model_text_apart():
    tracker = mod_tools.ActionTracker(
        timeouts=[{"user_id": "1", "username": "alice", "duration": "10m", "reason": "r"}],
        deletions=[{"message_id": "9", "reason": "r", "possibly_applied": True}],
    )
    embed = mod_tools.build_triage_report_embed(tracker, "I banned everyone.", "reporter", "3", "4")
    fields = {f.name: f.value for f in embed.fields}

    assert embed.description.startswith("**AI assessment (unverified):** I banned everyone.")
    assert "alice" in fields["Actions Taken (confirmed)"]
    assert "`9`" not in fields["Actions Taken (confirmed)"]
    assert "`9`" in fields["Possibly Applied (outcome unknown)"]
