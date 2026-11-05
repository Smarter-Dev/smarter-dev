"""Staff-ping triage safety, against a fake Discord guild.

Covers what #237 asked of the triage path:

* an empty ``enabled_tools`` offers no destructive tool and makes zero
  destructive Discord calls, whatever the model asks for;
* the bot reads the guild's config on each ping (no startup cache), and each
  destructive tool re-reads it right before acting, so switching triage off or
  removing a tool mid-run stops the action;
* every timeout, purge and delete target goes through the shared
  staff-protection rule, unknown counting as protected;
* an action slot is reserved before the Discord call and kept whatever the
  outcome; refusals before the call take none.

The model is scripted; the config row, the audit write and Discord are fakes.
"""

from __future__ import annotations

import contextlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import hikari
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot import triage_config
from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.agents import moderation_agent
from smarter_dev.bot.plugins import mod_monitor
from smarter_dev.bot.purge_core import DISCORD_EPOCH_MS
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.guild_rules import parse_guild_rules
from tests.bot.fake_discord import BOT_ID
from tests.bot.fake_discord import GUILD_ID
from tests.bot.fake_discord import OWNER_ID
from tests.bot.fake_discord import FakeDiscord
from tests.bot.fake_discord import overwrite
from tests.bot.fake_discord import server_error

CHANNEL_ID = 5000
REPORT_CHANNEL_ID = 424200000000000042
MOD_ROLE_ID = 644390354157568014  # pinging it starts triage
SPAMMER = 3001
HELPER = 3002  # an ordinary member with a non-staff role
STAFF_BY_ROLE = 3003
STAFF_BY_PERMISSION = 3004
STAFF_IN_ONE_CHANNEL = 3005
OTHER_BOT = 3006
HELPER_ROLE_ID = 7001
JANITOR_ROLE_ID = 7002  # Manage Messages, nothing else
EXEMPT_ROLE_ID = 700300000000000003  # no permissions; staff only by the policy


def snowflake(minutes_ago: float, salt: int = 0) -> int:
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return ((int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22) + salt


@pytest.fixture(autouse=True)
def _model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


@pytest.fixture
def discord() -> FakeDiscord:
    discord = FakeDiscord()
    discord.roles.update(
        {
            HELPER_ROLE_ID: hikari.Permissions.SEND_MESSAGES,
            JANITOR_ROLE_ID: hikari.Permissions.MANAGE_MESSAGES,
            MOD_ROLE_ID: hikari.Permissions.NONE,
            EXEMPT_ROLE_ID: hikari.Permissions.NONE,
        }
    )
    discord.channels.append(
        SimpleNamespace(
            id=5001,
            permission_overwrites={
                STAFF_IN_ONE_CHANNEL: overwrite(
                    allow=hikari.Permissions.MODERATE_MEMBERS
                )
            },
        )
    )
    discord.add_member(SPAMMER, username="spammer")
    discord.add_member(HELPER, role_ids=(HELPER_ROLE_ID,), username="helper")
    discord.add_member(STAFF_BY_ROLE, role_ids=(MOD_ROLE_ID,), username="mod")
    discord.add_member(
        STAFF_BY_PERMISSION, role_ids=(JANITOR_ROLE_ID,), username="janitor"
    )
    discord.add_member(STAFF_IN_ONE_CHANNEL, username="channel_mod")
    discord.add_member(OTHER_BOT, is_bot=True, username="otherbot")
    return discord


@pytest.fixture
def stored(monkeypatch):
    """The guild's moderation policy (#242: triage and the extra staff roles
    are only the policy's), read through the real loaders. ``row`` and
    ``filter_row`` are the policy's triage section and protected roles, as
    an admin edits them; ``policy`` replaces the whole document."""
    state = SimpleNamespace(
        row=SimpleNamespace(
            is_active=True,
            monitored_role_ids=[str(MOD_ROLE_ID)],
            instructions="Be fair.",
            enabled_tools=["timeout", "purge", "delete"],
            context_message_limit=25,
            response_channel_id=str(REPORT_CHANNEL_ID),
        ),
        filter_row=SimpleNamespace(staff_exempt_role_ids=[]),
        reads=0,
        fail=None,
        filter_fail=None,
        policy=None,
    )

    def document():
        if state.policy is not None:
            return state.policy
        raw = mp.default_document().as_json()
        raw["protected_role_ids"] = list(state.filter_row.staff_exempt_role_ids)
        raw["triage"].update(
            mode=mp.ENFORCE if state.row.is_active else mp.OFF,
            monitored_role_ids=list(state.row.monitored_role_ids),
            instructions=state.row.instructions,
            enabled_tools=list(state.row.enabled_tools or []),
            context_message_limit=state.row.context_message_limit,
            response_channel_id=state.row.response_channel_id,
        )
        return mp.validate(raw)

    @contextlib.asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def read_policy(session, guild_id):
        state.reads += 1
        if state.fail is not None:
            raise state.fail
        if guild_id != str(GUILD_ID):
            return None
        return SimpleNamespace(document=document())

    real_staff_roles = mod_tools.staff_exempt_role_ids

    async def staff_roles(guild_id):
        if state.filter_fail is not None:
            raise state.filter_fail
        return await real_staff_roles(guild_id)

    monkeypatch.setattr(triage_config, "read_policy", read_policy)
    monkeypatch.setattr(triage_config, "get_db_session_context", session)
    monkeypatch.setattr(mod_tools, "staff_exempt_role_ids", staff_roles)
    return state


class _Rows:
    """The ModerationAction rows the gateway wrote, read on demand."""

    def __init__(self, database) -> None:
        self._database = database

    async def all(self) -> list:
        from sqlalchemy import select

        from smarter_dev.web.models import ModerationAction

        async with self._database() as session:
            return list((await session.execute(select(ModerationAction))).scalars())


@pytest.fixture(autouse=True)
def audit(monkeypatch, moderation_gateway_db) -> _Rows:
    """The gateway is real (a throwaway database); its announcements are dropped."""
    from smarter_dev.bot import moderation_port

    async def ignore(*args, **kwargs):
        return None

    monkeypatch.setattr(moderation_port, "dispatch_mod_action", ignore)
    return _Rows(moderation_gateway_db)


def _tools(discord, enabled=("timeout", "purge", "delete")):
    """The triage tools with the real config gate (a fresh DB read)."""
    tools, tracker = mod_tools.create_moderation_tools(
        discord,
        guild_id=str(GUILD_ID),
        channel_id=str(CHANNEL_ID),
        trigger_message_id="1",
        enabled_tools=list(enabled) if enabled is not None else None,
    )
    return {tool.name: tool.function for tool in tools}, tracker


def _scripted(*steps, seen: list | None = None) -> FunctionModel:
    """Each step is one model turn's tool calls, or a callable run first that
    returns them; "final" is the output tool."""
    turns = list(steps)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append((messages, info))
        step = turns.pop(0)
        calls = step() if callable(step) else step
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name if name == "final" else name, args
                )
                for name, args in calls
            ]
        )

    return FunctionModel(respond)


def _returns(seen: list) -> list:
    return [
        part.content
        for messages, _ in seen
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


ALL_DESTRUCTIVE_CALLS = [
    ("timeout_user", {"user_id": str(SPAMMER), "duration": "10m", "reason": "spam"}),
    ("purge_messages", {"user_id": str(SPAMMER), "count": 5, "reason": "spam"}),
    ("delete_message", {"message_id": "424242", "reason": "spam"}),
]


# -- empty enabled_tools ---------------------------------------------------------


@pytest.mark.parametrize("stored_tools", [[], None])
async def test_an_empty_or_unknown_tool_list_makes_zero_destructive_calls(
    discord, stored, stored_tools, monkeypatch
):
    """The whole staff-ping path, from the listener to the report."""
    stored.row.enabled_tools = stored_tools
    discord.add_message(424242, SPAMMER, content="buy followers")
    seen: list = []
    model = _scripted(
        ALL_DESTRUCTIVE_CALLS,
        [("final", {"assessment": "Spam; wanted to act."})],
        seen=seen,
    )

    async def no_context(*args, **kwargs):
        return []

    monkeypatch.setattr(mod_monitor, "gather_message_context", no_context)
    monkeypatch.setattr(mod_monitor.plugin, "_app", discord)
    event = SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message=SimpleNamespace(
            id=1,
            author=SimpleNamespace(id=HELPER, is_bot=False, username="helper"),
            content="<@&644390354157568014> spam here",
            role_mention_ids=[MOD_ROLE_ID],
        ),
    )
    config = await triage_config.load_triage_config(str(GUILD_ID))
    assert config.enabled_tools == ()

    with moderation_agent.get_moderation_agent().override(model=model):
        await mod_monitor._handle_moderation(event, config)

    offered = {tool.name for tool in seen[0][1].function_tools}
    assert offered.isdisjoint({"timeout_user", "purge_messages", "delete_message"})
    assert discord.destructive == []
    # The run still reviews and reports.
    assert any(channel == str(REPORT_CHANNEL_ID) for channel, _, _ in discord.sent)


@pytest.mark.parametrize("enabled", [[], None])
def test_the_factory_offers_no_destructive_tool_for_an_empty_list(discord, enabled):
    tools, _ = _tools(discord, enabled)

    assert set(tools) == {
        "flag_users",
        "send_mod_message",
        "get_user_info",
        "get_user_history",
    }


def test_the_monitor_reads_an_empty_list_as_no_tools():
    assert triage_config.enabled_destructive_tools([]) == ()
    assert triage_config.enabled_destructive_tools(None) == ()
    assert triage_config.enabled_destructive_tools(["delete", "ban"]) == ("delete",)


# -- config freshness ------------------------------------------------------------


def _ping(author_id=HELPER, role_ids=(MOD_ROLE_ID,)):
    return SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message=SimpleNamespace(
            id=11,
            author=SimpleNamespace(id=author_id, is_bot=False, username="helper"),
            content="mods?",
            role_mention_ids=list(role_ids),
        ),
    )


async def test_a_saved_change_reaches_the_very_next_ping(stored, monkeypatch):
    started: list = []

    async def handle(event, config, *_):
        started.append(config)

    # Each ping a minute apart, so none joins an earlier one's incident (#384).
    now = iter(range(0, 600, 60))
    monkeypatch.setattr(mod_monitor, "_bursts", {})
    monkeypatch.setattr(mod_monitor, "_clock", lambda: next(now))
    monkeypatch.setattr(mod_monitor, "_handle_moderation", handle)
    monkeypatch.setattr(mod_monitor.leadership, "track", lambda task: task)

    await mod_monitor.on_message_create(_ping())
    stored.row.instructions = "New rules."
    stored.row.enabled_tools = ["delete"]
    await mod_monitor.on_message_create(_ping())
    stored.row.monitored_role_ids = ["999999999999999999"]
    await mod_monitor.on_message_create(_ping())
    stored.row.monitored_role_ids = [str(MOD_ROLE_ID)]
    stored.row.is_active = False
    await mod_monitor.on_message_create(_ping())
    await _drain()

    assert [c.instructions for c in started] == ["Be fair.", "New rules."]
    assert started[1].enabled_tools == ("delete",)


async def test_messages_without_a_role_mention_never_read_the_config(stored):
    await mod_monitor.on_message_create(_ping(role_ids=()))

    assert stored.reads == 0


async def test_an_unreadable_config_starts_no_triage(stored, monkeypatch):
    started: list = []

    async def handle(event, config, *_):
        started.append(config)

    monkeypatch.setattr(mod_monitor, "_handle_moderation", handle)
    stored.fail = RuntimeError("db down")

    await mod_monitor.on_message_create(_ping())
    await _drain()

    assert started == []


async def _drain():
    import asyncio

    for _ in range(3):
        await asyncio.sleep(0)


async def test_switching_triage_off_mid_run_stops_the_next_action(discord, stored):
    tools, tracker = _tools(discord)
    assert (await tools["timeout_user"](str(SPAMMER), "10m", "spam"))["success"] is True

    stored.row.is_active = False
    result = await tools["timeout_user"](str(HELPER), "10m", "spam")

    assert result["success"] is False
    assert "switched off" in result["error"]
    assert [call[1] for call in discord.destructive] == [SPAMMER]


async def test_removing_a_tool_mid_run_stops_it(discord, stored):
    discord.add_message(424242, SPAMMER)
    tools, _ = _tools(discord)

    stored.row.enabled_tools = ["timeout"]
    result = await tools["delete_message"]("424242", "spam")

    assert result["success"] is False
    assert "not enabled" in result["error"]
    assert discord.destructive == []


async def test_an_unreadable_config_before_an_action_refuses_it(discord, stored):
    tools, _ = _tools(discord)

    stored.fail = RuntimeError("db down")
    result = await tools["timeout_user"](str(SPAMMER), "10m", "spam")

    assert result["success"] is False
    assert discord.destructive == []


# -- staff protection ------------------------------------------------------------


PROTECTED = [
    pytest.param(STAFF_BY_ROLE, id="staff role"),
    pytest.param(STAFF_BY_PERMISSION, id="manage messages via a role"),
    pytest.param(OWNER_ID, id="owner"),
    pytest.param(OTHER_BOT, id="another bot"),
    pytest.param(BOT_ID, id="the bot itself"),
]


@pytest.mark.parametrize("target", PROTECTED)
async def test_protected_members_are_never_timed_out(discord, stored, target):
    tools, tracker = _tools(discord)

    result = await tools["timeout_user"](str(target), "10m", "spam")

    assert result["success"] is False
    assert result["error"].startswith("Cannot moderate this user")
    assert discord.destructive == []
    assert tracker.timeouts == []


@pytest.mark.parametrize("target", PROTECTED)
async def test_protected_members_are_never_purged(discord, stored, target):
    discord.add_message(snowflake(1, 1), target)
    tools, _ = _tools(discord)

    result = await tools["purge_messages"](str(target), 10, "spam")

    assert result["success"] is False
    assert discord.destructive == []


@pytest.mark.parametrize("target", PROTECTED)
async def test_a_protected_members_message_is_never_deleted(discord, stored, target):
    discord.add_message(424242, target)
    tools, _ = _tools(discord)

    result = await tools["delete_message"]("424242", "spam")

    assert result["success"] is False
    assert discord.destructive == []


@pytest.mark.parametrize("is_bot", [False, True])
async def test_a_webhook_message_is_deleted(discord, stored, is_bot):
    # A scam posted through a leaked webhook: no member behind it to protect.
    discord.add_message(424242, 88888, webhook_id=88888, is_bot=is_bot)
    tools, tracker = _tools(discord)

    result = await tools["delete_message"]("424242", "scam")

    assert result["success"] is True
    assert discord.destructive == [("delete_message", 424242)]
    assert [d["message_id"] for d in tracker.deletions] == ["424242"]


async def test_the_bots_own_interaction_response_is_not_deleted(discord, stored):
    discord.add_message(424242, BOT_ID, webhook_id=BOT_ID)
    tools, _ = _tools(discord)

    result = await tools["delete_message"]("424242", "spam")

    assert result["success"] is False
    assert discord.destructive == []


@pytest.mark.parametrize("configured", [False, True])
async def test_a_role_the_guild_configured_as_staff_exempt_protects(
    discord, stored, configured
):
    discord.add_member(3010, role_ids=(EXEMPT_ROLE_ID,), username="exempt")
    discord.add_message(424242, 3010)
    if configured:
        stored.filter_row.staff_exempt_role_ids = [str(EXEMPT_ROLE_ID)]
    tools, _ = _tools(discord)

    results = [
        await tools["timeout_user"]("3010", "10m", "spam"),
        await tools["purge_messages"]("3010", 10, "spam"),
        await tools["delete_message"]("424242", "spam"),
    ]

    if configured:
        assert [r["success"] for r in results] == [False, False, False]
        assert all("holds a staff role" in r["error"] for r in results)
        assert discord.destructive == []
    else:
        assert [r["success"] for r in results] == [True, True, True]


async def test_unreadable_staff_exempt_roles_refuse(discord, stored):
    stored.filter_fail = RuntimeError("db down")
    tools, _ = _tools(discord)

    result = await tools["timeout_user"](str(SPAMMER), "10m", "spam")

    assert result["success"] is False
    assert "could not be checked" in result["error"]
    assert discord.destructive == []


async def test_a_member_who_cannot_be_checked_is_left_alone(discord, stored):
    discord.cached = False
    discord.fail["fetch_guild_channels"] = server_error()
    tools, _ = _tools(discord)

    result = await tools["timeout_user"](str(SPAMMER), "10m", "spam")

    assert result["success"] is False
    assert "could not be checked" in result["error"]
    assert discord.destructive == []


async def test_purge_deletes_only_the_targets_messages_with_staff_in_the_channel(
    discord, stored, audit
):
    spam = [snowflake(3, 1), snowflake(2, 2)]
    staff_reply = snowflake(1.5, 3)
    discord.add_message(spam[0], SPAMMER)
    discord.add_message(staff_reply, STAFF_BY_ROLE)
    discord.add_message(spam[1], SPAMMER)
    tools, tracker = _tools(discord)

    result = await tools["purge_messages"](str(SPAMMER), 10, "spam")

    assert result["success"] is True
    assert discord.destructive == [("delete_messages", list(reversed(spam)))]
    assert [row.action_type for row in await audit.all()] == ["purge"]


async def test_a_departed_spammers_messages_can_still_be_purged(discord, stored):
    discord.users[4004] = SimpleNamespace(id=4004, is_bot=False)
    discord.add_message(snowflake(1, 1), 4004)
    tools, _ = _tools(discord)

    result = await tools["purge_messages"]("4004", 10, "spam")

    assert result["success"] is True
    [(kind, _)] = discord.destructive
    assert kind == "delete_message"  # one message goes through the single delete


async def test_an_ordinary_member_is_timed_out_and_recorded(discord, stored, audit):
    tools, tracker = _tools(discord)

    result = await tools["timeout_user"](str(HELPER), "30m", "harassment")

    assert result["success"] is True
    [(kind, user_id, kwargs)] = discord.destructive
    assert (kind, user_id) == ("edit_member", HELPER)
    assert [row.action_type for row in await audit.all()] == ["timeout"]
    assert tracker.timeouts[0]["user_id"] == str(HELPER)


@pytest.mark.parametrize("duration", ["0m", "2h"])
async def test_out_of_range_durations_are_refused_before_any_call(
    discord, stored, duration
):
    tools, _ = _tools(discord)

    result = await tools["timeout_user"](str(SPAMMER), duration, "spam")

    assert result["success"] is False
    assert discord.destructive == []


# -- the action budget -----------------------------------------------------------


async def test_a_failed_call_keeps_its_slot_and_is_reported_as_possibly_applied(
    discord, stored, audit
):
    discord.fail["edit_member"] = server_error()
    tools, tracker = _tools(discord)

    for _ in range(3):
        result = await tools["timeout_user"](str(SPAMMER), "10m", "spam")
        assert result["success"] is False
        assert result["error"].startswith("Outcome unknown")
    fourth = await tools["timeout_user"](str(SPAMMER), "10m", "spam")

    assert "Action limit reached" in fourth["error"]
    # The gateway sends the timeout once: the repeats are the same request,
    # whose outcome is unknown, and an unknown outcome is never resent blindly.
    assert len(discord.destructive) == 1
    assert all(entry["possibly_applied"] for entry in tracker.timeouts)
    assert await audit.all() == []


async def test_refusals_take_no_slot(discord, stored):
    tools, _ = _tools(discord)

    for target in (STAFF_BY_ROLE, OWNER_ID, OTHER_BOT):
        await tools["timeout_user"](str(target), "10m", "spam")
    results = [
        await tools["timeout_user"](str(user), "10m", "spam")
        for user in (SPAMMER, HELPER, SPAMMER)
    ]

    assert all(r["success"] for r in results)
    # The second timeout of the spammer is already covered by the first.
    assert len(discord.destructive) == 2


async def test_a_call_cancelled_in_flight_keeps_its_slot(discord, stored):
    import asyncio

    tools, tracker = _tools(discord)
    discord.fail["edit_member"] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await tools["timeout_user"](str(SPAMMER), "10m", "spam")
    del discord.fail["edit_member"]
    results = [
        await tools["timeout_user"](str(HELPER), "10m", "spam") for _ in range(3)
    ]

    assert tracker.timeouts[0]["possibly_applied"] is True
    assert [r["success"] for r in results] == [True, True, False]
    assert "Action limit reached" in results[2]["error"]


async def test_a_triage_run_with_all_tools_acts_through_the_real_path(discord, stored):
    discord.add_message(424242, SPAMMER, content="spam")
    seen: list = []
    model = _scripted(
        [ALL_DESTRUCTIVE_CALLS[0]],
        [
            (
                "timeout_user",
                {"user_id": str(STAFF_BY_ROLE), "duration": "10m", "reason": "x"},
            )
        ],
        [("final", {"assessment": "Timed out the spammer."})],
        seen=seen,
    )

    with moderation_agent.get_moderation_agent().override(model=model):
        assessment, tracker = await moderation_agent.run_moderation_agent(
            bot=discord,
            guild_id=str(GUILD_ID),
            channel_id=str(CHANNEL_ID),
            trigger_message_content="mods, spam",
            trigger_author="helper",
            context_messages=[],
            guild_instructions="",
            enabled_tools=["timeout", "purge", "delete"],
            trigger_message_id="1",
        )

    assert [call[1] for call in discord.destructive] == [SPAMMER]
    assert any(
        isinstance(r, dict)
        and r.get("error", "").startswith("Cannot moderate this user")
        for r in _returns(seen)
    )


async def test_under_a_policy_triage_reads_its_triage_section(discord, stored):
    """#241: ``evaluate`` (or enforcement switched off) leaves triage running
    with no destructive tool, and an empty ``enabled_tools`` is no tool."""
    raw = mp.default_document().as_json()
    raw["triage"].update(
        mode=mp.ENFORCE,
        monitored_role_ids=[str(MOD_ROLE_ID)],
        enabled_tools=["timeout"],
    )
    stored.policy = mp.validate(raw)
    tools, _ = _tools(discord)
    assert (await tools["timeout_user"](str(SPAMMER), "10m", "spam"))["success"] is True
    discord.add_message(424242, SPAMMER)
    refused = await tools["delete_message"]("424242", "spam")
    assert refused["success"] is False and "not enabled" in refused["error"]

    raw["triage"]["mode"] = mp.EVALUATE
    stored.policy = mp.validate(raw)
    assert (await tools["timeout_user"](str(HELPER), "10m", "spam"))["success"] is False
    raw["triage"].update(mode=mp.ENFORCE, enabled_tools=[])
    stored.policy = mp.validate(raw)
    assert (await tools["timeout_user"](str(HELPER), "10m", "spam"))["success"] is False
    raw["triage"]["mode"] = mp.OFF
    stored.policy = mp.validate(raw)
    result = await tools["timeout_user"](str(HELPER), "10m", "spam")
    assert result["success"] is False and "switched off" in result["error"]
    assert [call[1] for call in discord.destructive] == [SPAMMER]


async def test_without_a_policy_there_is_no_triage_and_no_extra_staff(
    discord, stored, monkeypatch
):
    """#242: the old moderation_configs row and the filter config's staff
    roles are gone; a guild without a policy has no triage, and only the
    built-in staff rule protects."""

    async def no_policy(session, guild_id):
        return None

    monkeypatch.setattr(triage_config, "read_policy", no_policy)
    assert await triage_config.load_triage_config(str(GUILD_ID)) is None
    assert await triage_config.staff_exempt_role_ids(str(GUILD_ID)) == ()
    tools, _ = _tools(discord)
    result = await tools["timeout_user"](str(SPAMMER), "10m", "spam")
    assert result["success"] is False and "switched off" in result["error"]
    assert discord.destructive == []


# -- server rules: cite, then delete (#466) --------------------------------------

RECRUITER = 3101
RECRUIT_MESSAGE = 424300
PING_MESSAGE = 424301
RULES = parse_guild_rules(
    "## Be respectful\nNo harassment.\n\n"
    "## No self-promotion\nNo ads, recruitment or unsolicited DMs.\n"
)


def _incident(discord):
    """A recruitment post from a young account, then a bare @mods ping."""
    discord.add_member(RECRUITER, username="recruiter")
    discord.add_message(
        RECRUIT_MESSAGE,
        RECRUITER,
        content="DM me about additional income, a partnership, you need a PC",
    )
    discord.add_message(PING_MESSAGE, HELPER, content="@mods ^")
    stamp = datetime.now(UTC)
    return [
        moderation_agent.DiscordMessage(
            author="recruiter",
            author_id=str(RECRUITER),
            message_id=str(RECRUIT_MESSAGE),
            timestamp=stamp,
            content="DM me about additional income, a partnership, you need a PC",
        ),
        moderation_agent.DiscordMessage(
            author="helper",
            author_id=str(HELPER),
            message_id=str(PING_MESSAGE),
            timestamp=stamp,
            content="@mods ^",
        ),
    ]


async def _triage_incident(discord, model, **kw):
    kwargs = {
        "bot": discord,
        "guild_id": str(GUILD_ID),
        "channel_id": str(CHANNEL_ID),
        "trigger_message_content": "@mods ^",
        "trigger_author": "helper",
        "trigger_author_id": str(HELPER),
        "context_messages": _incident(discord),
        "guild_instructions": "",
        "enabled_tools": ["timeout", "purge", "delete"],
        "trigger_message_id": str(PING_MESSAGE),
        "guild_rules": RULES,
        "enforcing": True,
        **kw,
    }
    with moderation_agent.get_moderation_agent().override(model=model):
        return await moderation_agent.run_moderation_agent(**kwargs)


CITE_AND_DELETE = [
    [
        (
            "cite_rule",
            {
                "message_id": str(RECRUIT_MESSAGE),
                "rule_indexes": [2],
                "reason": "Recruitment pitch asking for DMs.",
            },
        )
    ],
    [
        (
            "delete_message",
            {"message_id": str(RECRUIT_MESSAGE), "reason": "Unsolicited recruitment"},
        )
    ],
    [("send_mod_message", {"message": "I've removed that post."})],
    [("final", {"assessment": "MLM-style recruitment; cited and removed."})],
]


async def test_a_recruitment_post_and_a_bare_ping_gets_a_cited_rule_and_a_delete(
    discord, stored, audit
):
    seen: list = []

    _, tracker = await _triage_incident(discord, _scripted(*CITE_AND_DELETE, seen=seen))

    assert tracker.failure is None
    prompt = "\n".join(
        part.content
        for messages, _ in seen[:1]
        for message in messages
        for part in message.parts
        if hasattr(part, "content") and isinstance(part.content, str)
    )
    assert "## server_rules\n1. Be respectful" in prompt
    assert "2. No self-promotion" in prompt
    assert f"[msg:{RECRUIT_MESSAGE}]" in prompt
    # The rule goes out verbatim as a silent reply, before the delete.
    (channel, content, kwargs) = discord.sent[0]
    assert channel == str(CHANNEL_ID)
    assert "**2. No self-promotion**" in content and "No ads, recruitment" in content
    assert "Be respectful" not in content
    assert kwargs["reply"].id == RECRUIT_MESSAGE
    assert kwargs["mentions_reply"] is False
    assert discord.destructive == [("delete_message", RECRUIT_MESSAGE)]
    assert tracker.cites == [
        {
            "message_id": str(RECRUIT_MESSAGE),
            "rule_indexes": [2],
            "reason": "Recruitment pitch asking for DMs.",
        }
    ]
    assert tracker.deletions[0]["message_id"] == str(RECRUIT_MESSAGE)
    notes = [text for _, text in mod_tools.triage_notes(tracker, "x")]
    assert any("Cited rule(s) 2" in text for text in notes)


async def test_without_the_delete_tool_the_rule_is_still_cited(discord, stored):
    stored.row.enabled_tools = []
    steps = [CITE_AND_DELETE[0], CITE_AND_DELETE[2], CITE_AND_DELETE[3]]

    _, tracker = await _triage_incident(
        discord, _scripted(*steps), enabled_tools=[]
    )

    assert len(tracker.cites) == 1
    assert discord.destructive == []
    notes = [text for _, text in mod_tools.triage_notes(tracker, "x")]
    assert not any("no immediate action" in text for text in notes)


async def test_no_rules_means_no_cite_tool(discord, stored):
    seen: list = []

    await _triage_incident(
        discord, _scripted([("final", {"assessment": "ok"})], seen=seen), guild_rules=()
    )

    assert "cite_rule" not in {tool.name for tool in seen[0][1].function_tools}
    prompt = str(seen[0][0])
    assert "No server rules are configured." in prompt


@pytest.mark.parametrize(
    ("message_id", "rule_indexes", "expect"),
    [
        (str(RECRUIT_MESSAGE), [9], "do not exist"),
        (str(RECRUIT_MESSAGE), [0], "do not exist"),
        (str(RECRUIT_MESSAGE), [], "at least one rule"),
        ("999999", [2], "not one of the messages"),
        (str(PING_MESSAGE), [2], "not one of the messages"),  # the ping itself
    ],
)
async def test_cite_rule_refuses_what_validation_rejects(
    discord, stored, message_id, rule_indexes, expect
):
    tools, tracker = mod_tools.create_moderation_tools(
        discord,
        guild_id=str(GUILD_ID),
        channel_id=str(CHANNEL_ID),
        trigger_message_id=str(PING_MESSAGE),
        enabled_tools=[],
        rules=RULES,
        enforcing=True,
        context_messages=_incident(discord),
    )
    cite = next(t.function for t in tools if t.name == "cite_rule")

    result = await cite(message_id, rule_indexes, "why")

    assert result["success"] is False and expect in result["error"]
    assert discord.sent == [] and tracker.cites == []


async def test_cite_rule_never_cites_the_bot_or_staff(discord, stored):
    discord.add_message(424400, BOT_ID, content="a bot reply")
    discord.add_message(424401, STAFF_BY_ROLE, content="mod talking")
    stamp = datetime.now(UTC)
    context = [
        moderation_agent.DiscordMessage(
            author=name, author_id=str(uid), message_id=str(mid), timestamp=stamp,
            content=text,
        )
        for name, uid, mid, text in (
            ("smarter-dev", BOT_ID, 424400, "a bot reply"),
            ("mod", STAFF_BY_ROLE, 424401, "mod talking"),
        )
    ]
    tools, tracker = mod_tools.create_moderation_tools(
        discord,
        guild_id=str(GUILD_ID),
        channel_id=str(CHANNEL_ID),
        trigger_message_id=str(PING_MESSAGE),
        enabled_tools=[],
        rules=RULES,
        enforcing=True,
        context_messages=context,
    )
    cite = next(t.function for t in tools if t.name == "cite_rule")

    bot_result = await cite("424400", [1], "why")
    staff_result = await cite("424401", [1], "why")

    assert bot_result["success"] is False and "own message" in bot_result["error"]
    assert staff_result["success"] is False
    assert staff_result["error"].startswith("Cannot moderate this user")
    assert discord.sent == [] and tracker.cites == []


async def test_cite_rule_posts_each_message_once(discord, stored):
    tools, tracker = mod_tools.create_moderation_tools(
        discord,
        guild_id=str(GUILD_ID),
        channel_id=str(CHANNEL_ID),
        trigger_message_id=str(PING_MESSAGE),
        enabled_tools=[],
        rules=RULES,
        enforcing=True,
        context_messages=_incident(discord),
    )
    cite = next(t.function for t in tools if t.name == "cite_rule")

    first = await cite(str(RECRUIT_MESSAGE), [2], "why")
    again = await cite(str(RECRUIT_MESSAGE), [2], "why")

    assert first["success"] and again["success"]
    assert len(discord.sent) == 1 and len(tracker.cites) == 1


def _cite_tool(discord, **kw):
    tools, tracker = mod_tools.create_moderation_tools(
        discord,
        guild_id=str(GUILD_ID),
        channel_id=str(CHANNEL_ID),
        trigger_message_id=str(PING_MESSAGE),
        enabled_tools=kw.pop("enabled_tools", []),
        rules=RULES,
        context_messages=_incident(discord),
        **kw,
    )
    return {t.name: t.function for t in tools}, tracker


async def test_evaluate_mode_offers_no_cite_tool_and_posts_nothing(discord, stored):
    seen: list = []

    _, tracker = await _triage_incident(
        discord,
        _scripted([("final", {"assessment": "ok"})], seen=seen),
        enforcing=False,
    )

    assert "cite_rule" not in {tool.name for tool in seen[0][1].function_tools}
    assert discord.sent == [] and tracker.cites == []


async def test_cite_is_offered_whatever_enabled_tools_says(discord, stored):
    tools, _ = _cite_tool(discord, enforcing=True, enabled_tools=[])

    assert "cite_rule" in tools


async def test_cite_is_refused_when_enforcement_is_switched_off_mid_run(
    discord, stored
):
    raw = mp.default_document().as_json()
    raw["triage"].update(mode=mp.ENFORCE, monitored_role_ids=[str(MOD_ROLE_ID)])
    stored.policy = mp.validate(raw)
    tools, tracker = _cite_tool(discord, enforcing=True)
    raw["triage"]["mode"] = mp.EVALUATE
    stored.policy = mp.validate(raw)

    result = await tools["cite_rule"](str(RECRUIT_MESSAGE), [2], "why")

    assert result["success"] is False and "only posted while triage is enforcing" in result["error"]
    assert discord.sent == [] and tracker.cites == []

    raw["triage"]["mode"] = mp.OFF
    stored.policy = mp.validate(raw)
    result = await tools["cite_rule"](str(RECRUIT_MESSAGE), [2], "why")
    assert result["success"] is False and discord.sent == []


async def test_cite_that_times_out_is_possibly_posted_and_not_retried(discord, stored):
    raw = mp.default_document().as_json()
    raw["triage"].update(mode=mp.ENFORCE, monitored_role_ids=[str(MOD_ROLE_ID)])
    stored.policy = mp.validate(raw)
    tools, tracker = _cite_tool(discord, enforcing=True)
    discord.fail["create_message"] = TimeoutError("slow")

    first = await tools["cite_rule"](str(RECRUIT_MESSAGE), [2], "why")
    discord.fail.clear()
    retry = await tools["cite_rule"](str(RECRUIT_MESSAGE), [2], "why")

    assert first["success"] is False
    assert retry["success"] is False and "may already be posted" in retry["error"]
    assert discord.sent == []
    assert tracker.cites[0]["possibly_applied"] is True
    notes = [text for _, text in mod_tools.triage_notes(tracker, "x")]
    assert any("Possibly cited" in text for text in notes)
