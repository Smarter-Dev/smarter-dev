"""Back-to-back staff pings from one member are one incident (#384).

On Oct 25 2026 a member sent ``@Admin`` then ``@mods`` seconds apart; each
ping opened its own incident and ran its own triage, and the first run could
not look the member up because it had their username and no id. These run the
real listener over the real gateway (SQLite, fake Discord); only the triage
model, or the whole triage run, is scripted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from smarter_dev.bot import leadership
from smarter_dev.bot.agents import moderation_agent
from smarter_dev.bot.agents.mod_tools import ActionTracker
from smarter_dev.bot.plugins import mod_monitor
from smarter_dev.bot.triage_config import TriageConfig
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import types as gw
from tests.moderation_fakes import GUILD
from tests.moderation_fakes import REPORT_CHANNEL
from tests.moderation_fakes import FakeModerationDiscord
from tests.moderation_fakes import gateway_session_factory

ROLE = "31337"
SHOWCASE = "7001"
OTHER_CHANNEL = "7002"
HAZE = "1556021025215348897"
SEIF = "5002"
NOTICE = "Moderator roles are for moderation issues; coding questions go in help."


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ping(message_id: int, content: str, *, author: str = HAZE, channel: str = SHOWCASE):
    message = SimpleNamespace(
        id=message_id,
        content=content,
        role_mention_ids=[int(ROLE)],
        author=SimpleNamespace(
            id=int(author),
            username="brave_armadillo_98165" if author == HAZE else f"member{author}",
            display_name="HAZE" if author == HAZE else None,
            is_bot=False,
        ),
        member=SimpleNamespace(display_name="HAZE" if author == HAZE else None),
    )
    return SimpleNamespace(
        message=message, guild_id=int(GUILD), channel_id=int(channel)
    )


class Triage:
    """Stands in for ``run_moderation_agent``: records each run, posts a notice."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, **kwargs):
        self.runs.append(kwargs)
        await self.release.wait()
        return "Two bare pings; no action.", ActionTracker(channel_message=NOTICE)


@pytest.fixture
async def world(tmp_path, monkeypatch):
    engine, db = await gateway_session_factory(tmp_path)
    discord = FakeModerationDiscord()
    gateway = ModerationGateway(discord, session_factory=db)
    clock = Clock()
    triage = Triage()
    bot = SimpleNamespace(rest=SimpleNamespace(create_message=AsyncMock()))

    async def config(guild_id):
        return TriageConfig(
            guild_id=guild_id,
            monitored_role_ids=frozenset({ROLE}),
            instructions="Warn members who ping staff for coding help.",
            enabled_tools=(),
            context_message_limit=10,
            response_channel_id=REPORT_CHANNEL,
        )

    monkeypatch.setattr(mod_monitor, "_bursts", {})
    monkeypatch.setattr(mod_monitor, "_clock", clock)
    monkeypatch.setattr(mod_monitor, "load_triage_config", config)
    monkeypatch.setattr(
        mod_monitor, "gather_message_context", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(mod_monitor, "bot_gateway", lambda _bot: gateway)
    monkeypatch.setattr(mod_monitor, "run_moderation_agent", triage)
    monkeypatch.setattr(mod_monitor.plugin, "_app", bot)
    yield SimpleNamespace(db=db, discord=discord, clock=clock, triage=triage, bot=bot)
    await engine.dispose()


async def send(event) -> None:
    await mod_monitor.on_message_create(event)


async def settle() -> None:
    assert await leadership.drain_tracked(5) == []


async def incidents(world) -> dict:
    """Each incident's id -> the message ids its evidence holds."""
    async with world.db() as session:
        ids = (await session.execute(select(ModerationIncident.id))).scalars().all()
        rows = (
            await session.execute(
                select(
                    ModerationIncidentEntry.incident_id,
                    ModerationIncidentEntry.message_id,
                ).where(ModerationIncidentEntry.kind == gw.ENTRY_EVIDENCE)
            )
        ).all()
    return {i: sorted(m for inc, m in rows if inc == i) for i in ids}


def notices(world) -> list[str]:
    return [call.args[1] for call in world.bot.rest.create_message.await_args_list]


async def test_two_pings_from_one_member_are_one_incident_and_one_run(world) -> None:
    world.triage.release.clear()  # the first run is still going when @mods lands
    await send(ping(101, "<@&31337> Admin"))
    world.clock.now += 4
    await send(ping(102, "<@&31337> mods"))
    await asyncio.sleep(0.05)
    world.triage.release.set()
    await settle()

    assert list((await incidents(world)).values()) == [["101", "102"]]
    assert len(world.triage.runs) == 1
    assert len(notices(world)) == 1
    assert len(world.discord.reports()) == 1
    report = world.discord.latest_report_text()
    assert "> <@&31337> Admin" in report and "> <@&31337> mods" in report
    assert f"/{SHOWCASE}/101" in report  # the incident is the first ping's
    assert "Another staff ping by brave_armadillo_98165: " in report


async def test_a_ping_after_the_run_finished_still_joins_within_the_window(
    world,
) -> None:
    await send(ping(101, "<@&31337> Admin"))
    await settle()
    world.clock.now += 59
    await send(ping(102, "<@&31337> mods"))
    await settle()

    assert list((await incidents(world)).values()) == [["101", "102"]]
    assert len(world.triage.runs) == 1
    assert len(notices(world)) == 1
    # The report was already final; the late ping still shows in it.
    assert "> <@&31337> mods" in world.discord.latest_report_text()


async def test_pings_from_two_members_are_two_incidents(world) -> None:
    await send(ping(101, "<@&31337> Admin"))
    await send(ping(102, "<@&31337> mods", author=SEIF))
    await settle()

    assert sorted((await incidents(world)).values()) == [["101"], ["102"]]
    assert len(world.triage.runs) == 2


async def test_pings_in_two_channels_are_two_incidents(world) -> None:
    await send(ping(101, "<@&31337> Admin"))
    await send(ping(102, "<@&31337> mods", channel=OTHER_CHANNEL))
    await settle()

    assert sorted((await incidents(world)).values()) == [["101"], ["102"]]
    assert len(world.triage.runs) == 2


async def test_a_ping_after_the_window_is_a_new_incident(world) -> None:
    await send(ping(101, "<@&31337> Admin"))
    await settle()
    world.clock.now += 60  # 60 s from the first ping, not from the last
    await send(ping(102, "<@&31337> mods"))
    await settle()

    assert sorted((await incidents(world)).values()) == [["101"], ["102"]]
    assert len(world.triage.runs) == 2


async def test_a_joining_ping_still_lands_when_the_first_run_failed(
    world, monkeypatch
) -> None:
    async def broken(*_args, **_kwargs):
        raise RuntimeError("context fetch failed")

    world.triage.release.clear()
    monkeypatch.setattr(mod_monitor, "gather_message_context", broken)
    await send(ping(101, "<@&31337> Admin"))
    await send(ping(102, "<@&31337> mods"))
    await settle()

    # The first ping never opened its incident; the second is still recorded.
    assert list((await incidents(world)).values()) == [["102"]]
    assert world.triage.runs == []


# -- the member's id reaches the triage ---------------------------------------


def _prompt(messages: list[ModelMessage]) -> str:
    return "\n".join(
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


async def test_the_triage_prompt_carries_the_pinging_members_id(
    world, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(
        mod_monitor, "run_moderation_agent", moderation_agent.run_moderation_agent
    )
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(_prompt(messages))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"assessment": "ok"})]
        )

    with moderation_agent.get_moderation_agent().override(model=FunctionModel(respond)):
        await send(ping(101, "<@&31337> Admin"))
        await settle()

    assert len(prompts) == 1
    assert f"## trigger_author_id\n{HAZE}" in prompts[0]
    assert "## trigger_author_display_name\nHAZE" in prompts[0]
    assert "## trigger_author\nbrave_armadillo_98165" in prompts[0]
