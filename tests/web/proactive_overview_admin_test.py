"""Admin overview of proactive channels: listing, and the on/off switch.

The switch reuses the web's one channel toggle, which does no authorization,
so most of these pin the page's own checks: admin guard, CSRF, the channel
filed under the guild, and Discord vouching for the channel before it goes
on. Rows live in the suite's SQLite database and Discord answers through an
``httpx.MockTransport`` behind the real admin client.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import fakeredis.aioredis
import httpx
import pytest
from litestar.response import Redirect
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from sqlalchemy import select

from smarter_dev.shared.proactive_channel_wake import CHANNEL_ENABLED_STREAM
from smarter_dev.shared.proactive_channel_wake import ChannelEnabledRequest
from smarter_dev.web.bot_admin import proactive_overview
from smarter_dev.web.bot_admin.proactive_overview import (
    ProactiveChannelsAdminController as Controller,
)
from smarter_dev.web.discord_admin_client import DiscordAdminClient
from smarter_dev.web.models import ProactiveChannelSettings
from tests.web.admin_template_rendering import admin_template_source
from tests.web.admin_template_rendering import render_admin_template

_MODULE = "smarter_dev.web.bot_admin.proactive_overview"
GUILD = "644299523686006834"
OTHER_GUILD = "700000000000000001"
GENERAL = "644299524151443487"
HELP = "644299524151443488"
OLD = "644299524151443489"
THREAD = "644299524151443490"
ELSEWHERE = "700000000000000011"

overview = Controller.overview.fn
toggle = Controller.toggle.fn


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


class _Discord:
    """A Discord that knows two guilds; flip ``down`` to make it unreachable."""

    def __init__(self):
        self.down = False
        self.paths: list[str] = []
        self.channel_guilds = {
            GENERAL: GUILD,
            HELP: GUILD,
            THREAD: GUILD,
            ELSEWHERE: OTHER_GUILD,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10")
        self.paths.append(path)
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if path == "/users/@me/guilds":
            return httpx.Response(
                200,
                json=[
                    {"id": GUILD, "name": "Smarter Dev", "icon": None},
                    {"id": OTHER_GUILD, "name": "Another Place", "icon": None},
                ],
            )
        if path == f"/guilds/{GUILD}/channels":
            return httpx.Response(
                200,
                json=[
                    {"id": GENERAL, "name": "general", "type": 0, "position": 0},
                    {"id": HELP, "name": "help", "type": 0, "position": 1},
                ],
            )
        if path.startswith("/guilds/"):
            return httpx.Response(500, json={"message": "boom"})
        if path.startswith("/channels/"):
            channel_id = path.removeprefix("/channels/")
            if channel_id not in self.channel_guilds:
                return httpx.Response(404, json={"message": "Unknown Channel", "code": 10003})
            return httpx.Response(
                200, json={"id": channel_id, "guild_id": self.channel_guilds[channel_id]}
            )
        return httpx.Response(404)

    def client(self) -> DiscordAdminClient:
        return DiscordAdminClient(bot_token="t", transport=httpx.MockTransport(self.handler))


# Not named "discord": conftest skips any test with that in a fixture name
# unless hikari is installed.
@pytest.fixture
def rest():
    return _Discord()


@pytest.fixture
def patched(redis, rest):
    flashes: list[tuple[str, str]] = []
    with (
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_MODULE}.get_flash_messages", return_value=[]),
        patch(f"{_MODULE}.flash_error", side_effect=lambda _r, m: flashes.append(("error", m))),
        patch(f"{_MODULE}.flash_success", side_effect=lambda _r, m: flashes.append(("success", m))),
        patch(f"{_MODULE}.get_redis_client", return_value=redis),
        patch(f"{_MODULE}.get_admin_discord_client", side_effect=rest.client),
        patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=True)),
    ):
        yield SimpleNamespace(flashes=flashes)


def _request(form: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(form=AsyncMock(return_value=form or {}), session={})


async def _seed(session, *rows: tuple[str, str, bool]) -> None:
    for guild_id, channel_id, enabled in rows:
        session.add(
            ProactiveChannelSettings(
                guild_id=guild_id,
                channel_id=channel_id,
                enabled=enabled,
                watch_addendum="keep",
            )
        )
    await session.commit()


async def _enabled(session, channel_id: str) -> bool | None:
    session.expire_all()
    row = await session.scalar(
        select(ProactiveChannelSettings).where(
            ProactiveChannelSettings.channel_id == channel_id
        )
    )
    return None if row is None else row.enabled


async def _wakes(redis) -> list[ChannelEnabledRequest]:
    return [
        ChannelEnabledRequest.model_validate_json(fields[b"payload"])
        for _id, fields in await redis.xrange(CHANNEL_ENABLED_STREAM)
    ]


async def _post(session, guild_id: str, channel_id: str, enabled: str):
    return await toggle(
        Controller,
        _request({"enabled": enabled}),
        session,
        guild_id=guild_id,
        channel_id=channel_id,
    )


# --- guards and wiring --------------------------------------------------------


@pytest.mark.parametrize("handler", [Controller.overview, Controller.toggle], ids=lambda h: h.handler_name)
def test_every_route_requires_the_administrator_permission(handler):
    assert auth_guard in handler.guards
    assert any(
        isinstance(guard, Permission) and guard.permission == "administrator"
        for guard in handler.guards
    )


def test_the_toggle_is_a_post():
    assert Controller.toggle.http_methods == {"POST"}


@pytest.mark.parametrize("config", ["app.yaml", "app.development.yaml"])
def test_the_controller_is_registered(config):
    text = (Path(__file__).resolve().parents[2] / config).read_text()
    assert "smarter_dev.web.bot_admin.proactive_overview:ProactiveChannelsAdminController" in text


def test_the_sidebar_links_the_page():
    assert 'href="/admin/bot/proactive-channels"' in admin_template_source(
        "admin/bot/_sidebar.html"
    )


# --- the listing --------------------------------------------------------------


async def test_the_page_lists_each_guilds_channels_on_first_with_names(db_session, patched):
    await _seed(
        db_session,
        (GUILD, HELP, True),
        (GUILD, OLD, False),
        (GUILD, GENERAL, True),
        (GUILD, THREAD, True),
        (OTHER_GUILD, ELSEWHERE, True),
    )

    response = await overview(Controller, _request(), db_session)

    assert response.template_name == "admin/bot/proactive_channels.html"
    sections = {section.guild_id: section for section in response.context["sections"]}
    ours = sections[GUILD]
    assert ours.name == "Smarter Dev"
    assert ours.enabled_count == 3
    assert [(row.channel_id, row.name, row.enabled) for row in ours.channels] == [
        (GENERAL, "general", True),
        (HELP, "help", True),
        (THREAD, None, True),  # threads are not in the guild's channel list
        (OLD, None, False),  # switched off, still listed so it can go back on
    ]
    assert not ours.names_unavailable
    # Discord refused that guild's channel list: its rows show IDs only.
    assert sections[OTHER_GUILD].names_unavailable
    assert sections[OTHER_GUILD].name == "Another Place"


async def test_the_page_shows_ids_when_discord_is_down(db_session, patched, rest):
    rest.down = True
    await _seed(db_session, (GUILD, GENERAL, True))

    response = await overview(Controller, _request(), db_session)

    (section,) = response.context["sections"]
    assert section.name is None
    assert section.names_unavailable
    assert section.channels[0].channel_id == GENERAL


async def test_the_page_renders_without_a_bot_token(db_session, patched):
    await _seed(db_session, (GUILD, GENERAL, True))
    with patch(
        f"{_MODULE}.get_admin_discord_client",
        side_effect=proactive_overview.DiscordAdminError("Discord bot token not configured"),
    ):
        response = await overview(Controller, _request(), db_session)
    (section,) = response.context["sections"]
    assert section.names_unavailable


def test_the_template_offers_the_opposite_switch_with_a_csrf_token():
    on = proactive_overview.ProactiveChannelRow(channel_id=GENERAL, name="general", enabled=True)
    off = proactive_overview.ProactiveChannelRow(channel_id=OLD, name=None, enabled=False)
    html = render_admin_template(
        "admin/bot/proactive_channels.html",
        sections=[
            proactive_overview.ProactiveGuildSection(
                guild_id=GUILD,
                name="Smarter Dev",
                channels=[on, off],
                enabled_count=1,
                names_unavailable=False,
            )
        ],
        flash_messages=[],
        active_page="proactive_channels",
        guild_id=None,
    )
    assert "#general" in html and "thread or deleted channel" in html
    assert f'action="/admin/bot/proactive-channels/{GUILD}/{GENERAL}"' in html
    assert f'action="/admin/bot/proactive-channels/{GUILD}/{OLD}"' in html
    assert html.count('name="csrf_token"') == 2
    assert '<input type="hidden" name="enabled" value="off">' in html
    assert '<input type="hidden" name="enabled" value="on">' in html
    assert "Turn off" in html and "Turn on" in html


def test_the_template_says_when_nothing_is_on():
    html = render_admin_template(
        "admin/bot/proactive_channels.html",
        sections=[],
        flash_messages=[],
        active_page="proactive_channels",
        guild_id=None,
    )
    assert "hasn't been switched on in any channel yet" in html


# --- switching ----------------------------------------------------------------


async def test_switching_off_saves_without_a_wake_or_discord(db_session, patched, redis, rest):
    await _seed(db_session, (GUILD, GENERAL, True))
    rest.down = True  # an outage never stops an admin turning it off

    response = await _post(db_session, GUILD, GENERAL, "off")

    assert isinstance(response, Redirect)
    assert response.url == "/admin/bot/proactive-channels"
    assert await _enabled(db_session, GENERAL) is False
    assert await _wakes(redis) == []
    assert not any(path.startswith("/channels/") for path in rest.paths)
    assert patched.flashes[-1][0] == "success"


async def test_switching_on_wakes_the_agent_once(db_session, patched, redis):
    await _seed(db_session, (GUILD, HELP, False))

    await _post(db_session, GUILD, HELP, "on")
    await _post(db_session, GUILD, HELP, "on")  # a double-click

    assert await _enabled(db_session, HELP) is True
    wakes = await _wakes(redis)
    assert [(wake.guild_id, wake.channel_id) for wake in wakes] == [(GUILD, HELP)]
    assert [level for level, _ in patched.flashes] == ["success", "success"]


async def test_switching_on_keeps_the_watch_addendum(db_session, patched):
    await _seed(db_session, (GUILD, GENERAL, False))
    await _post(db_session, GUILD, GENERAL, "on")
    db_session.expire_all()
    row = await db_session.scalar(
        select(ProactiveChannelSettings).where(ProactiveChannelSettings.channel_id == GENERAL)
    )
    assert (row.enabled, row.watch_addendum) == (True, "keep")


async def test_a_channel_filed_under_another_guild_is_refused(db_session, patched, redis):
    """The shared toggle keys on the channel alone; the page must not let a
    guild/channel mismatch switch another guild's channel."""
    await _seed(db_session, (OTHER_GUILD, ELSEWHERE, False), (GUILD, GENERAL, True))

    await _post(db_session, GUILD, ELSEWHERE, "on")
    await _post(db_session, OTHER_GUILD, GENERAL, "off")

    assert await _enabled(db_session, ELSEWHERE) is False
    assert await _enabled(db_session, GENERAL) is True
    assert await _wakes(redis) == []
    assert [level for level, _ in patched.flashes] == ["error", "error"]


async def test_a_channel_that_was_never_configured_is_refused(db_session, patched, redis):
    await _post(db_session, GUILD, HELP, "on")

    assert await _enabled(db_session, HELP) is None  # no row inserted
    assert await _wakes(redis) == []
    assert patched.flashes == [("error", "That channel isn't on this page. Reload and try again.")]


async def test_switching_on_is_refused_when_discord_puts_the_channel_elsewhere(
    db_session, patched, redis
):
    # Our row says GUILD, but Discord says the channel is the other guild's.
    await _seed(db_session, (GUILD, ELSEWHERE, False))

    await _post(db_session, GUILD, ELSEWHERE, "on")

    assert await _enabled(db_session, ELSEWHERE) is False
    assert await _wakes(redis) == []
    assert patched.flashes == [
        ("error", "That channel isn't in this server, so it was not switched on.")
    ]


async def test_switching_on_is_refused_for_a_channel_discord_no_longer_has(
    db_session, patched, redis
):
    await _seed(db_session, (GUILD, OLD, False))  # Discord answers 404 for OLD

    await _post(db_session, GUILD, OLD, "on")

    assert await _enabled(db_session, OLD) is False
    assert await _wakes(redis) == []
    assert "no longer exists" in patched.flashes[0][1]


async def test_switching_on_is_refused_while_discord_is_unreachable(
    db_session, patched, redis, rest
):
    await _seed(db_session, (GUILD, GENERAL, False))
    rest.down = True

    await _post(db_session, GUILD, GENERAL, "on")

    assert await _enabled(db_session, GENERAL) is False
    assert await _wakes(redis) == []
    assert patched.flashes == [
        ("error", "Couldn't reach Discord to check the channel. Please try again.")
    ]


async def test_switching_on_a_thread_checks_the_thread_itself(db_session, patched, redis):
    await _seed(db_session, (GUILD, THREAD, False))

    await _post(db_session, GUILD, THREAD, "on")

    assert await _enabled(db_session, THREAD) is True
    assert len(await _wakes(redis)) == 1


async def test_a_bad_csrf_token_is_refused(db_session, patched, redis):
    await _seed(db_session, (GUILD, GENERAL, True), (GUILD, HELP, False))
    with patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=False)):
        await _post(db_session, GUILD, GENERAL, "off")
        await _post(db_session, GUILD, HELP, "on")

    assert await _enabled(db_session, GENERAL) is True
    assert await _wakes(redis) == []
    assert await _enabled(db_session, HELP) is False
    assert [level for level, _ in patched.flashes] == ["error", "error"]

