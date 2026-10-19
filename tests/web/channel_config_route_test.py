"""Route-level tests for the signed-link channel configuration page.

The link is the only authorization, so these assert the security boundary:
a bad, expired or cross-channel link gets nothing, and a good one writes
exactly the channel it names.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from smarter_dev.shared import config_links
from smarter_dev.shared.proactive_channel_wake import CHANNEL_ENABLED_STREAM
from smarter_dev.web import proactive_channels
from smarter_dev.web.bot_admin import channel_config

# Litestar's decorators wrap these as route handlers; .fn is the function.
CONTROLLER = channel_config.ChannelConfigController
show = CONTROLLER.show.fn
save = CONTROLLER.save.fn

GUILD = "644299523686006834"
CHANNEL = "644299524151443487"
USER = "266000000000000001"


def _token(guild_id: str = GUILD, channel_id: str = CHANNEL) -> str:
    return config_links.sign_config_link(
        guild_id=guild_id, channel_id=channel_id, discord_user_id=USER
    )


def _request(form: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        form=AsyncMock(return_value=form or {}),
        session={},
    )


def _valid_form(**overrides) -> dict:
    fields = {
        "bot_kind": "proactive",
        "model_key": "",
        "reasoning_level": "",
        "daily_token_budget": "0",
        "hourly_token_budget": "0",
        "auto_respond": "",
        "fallback_model_key": "",
        "response_filter": "",
        "drafter_model": "",
    }
    fields.update(overrides)
    return fields


@pytest.fixture
def crud_mocks():
    get_proactive = AsyncMock(return_value=None)
    redis = fakeredis.aioredis.FakeRedis()
    with (
        patch.object(
            channel_config, "get_channel_model_override", new=AsyncMock(return_value=None)
        ) as get_override,
        patch.object(channel_config, "get_proactive_channel_settings", new=get_proactive),
        patch.object(
            channel_config, "upsert_channel_model_override", new=AsyncMock()
        ) as upsert_override,
        # The database decides whether this save turned the channel on.
        patch.object(
            proactive_channels,
            "set_proactive_channel_enabled_flag",
            new=AsyncMock(return_value=True),
        ) as set_flag,
        patch.object(channel_config, "get_redis_client", lambda: redis),
        patch.object(channel_config, "flash_success", lambda *a, **k: None),
        patch.object(channel_config, "flash_error", lambda *a, **k: None),
        patch.object(channel_config, "get_flash_messages", lambda request: []),
    ):
        yield SimpleNamespace(
            get_override=get_override,
            get_proactive=get_proactive,
            upsert_override=upsert_override,
            set_flag=set_flag,
            redis=redis,
        )


async def test_get_renders_the_form_for_a_valid_link(crud_mocks):
    response = await show(CONTROLLER,
        _request(), AsyncMock(), _token()
    )
    assert response.template_name == "admin/bot/channel_config.html"
    assert response.context["channel_id"] == CHANNEL
    crud_mocks.get_override.assert_awaited_once()


async def test_get_refuses_a_forged_link(crud_mocks):
    response = await show(CONTROLLER,
        _request(), AsyncMock(), "obviously-not-signed"
    )
    assert response.template_name == "admin/bot/channel_config_invalid.html"
    assert response.status_code == 403
    crud_mocks.get_override.assert_not_awaited()


async def test_post_writes_exactly_the_channel_the_link_names(crud_mocks):
    session = AsyncMock()

    response = await save(CONTROLLER,
        _request(_valid_form()), session, _token()
    )

    assert response.status_code in (302, 303, 307)
    override_kwargs = crud_mocks.upsert_override.await_args.kwargs
    assert override_kwargs["guild_id"] == GUILD
    assert override_kwargs["channel_id"] == CHANNEL
    flag_kwargs = crud_mocks.set_flag.await_args.kwargs
    assert (flag_kwargs["guild_id"], flag_kwargs["channel_id"]) == (GUILD, CHANNEL)
    assert flag_kwargs["enabled"] is True
    session.commit.assert_awaited_once()


async def test_post_refuses_an_expired_link(crud_mocks, monkeypatch):
    monkeypatch.setattr(
        channel_config,
        "verify_config_link",
        lambda token: None,  # what an expired link resolves to
    )
    response = await save(CONTROLLER,
        _request(_valid_form()), AsyncMock(), _token()
    )
    assert response.status_code == 403
    crud_mocks.upsert_override.assert_not_awaited()
    crud_mocks.set_flag.assert_not_awaited()


async def test_post_rejects_an_invalid_form_without_writing(crud_mocks):
    response = await save(CONTROLLER,
        _request(_valid_form(daily_token_budget="-5")), AsyncMock(), _token()
    )
    assert response.status_code in (302, 303, 307)
    crud_mocks.upsert_override.assert_not_awaited()


async def test_post_that_switches_a_channel_on_wakes_the_agent(crud_mocks):
    await save(CONTROLLER, _request(_valid_form()), AsyncMock(), _token())
    assert await crud_mocks.redis.xlen(CHANNEL_ENABLED_STREAM) == 1


async def test_post_for_a_channel_already_on_sends_no_wake(crud_mocks):
    crud_mocks.set_flag.return_value = False  # the row was already on
    await save(CONTROLLER, _request(_valid_form()), AsyncMock(), _token())
    assert await crud_mocks.redis.xlen(CHANNEL_ENABLED_STREAM) == 0


async def test_post_that_switches_a_channel_off_sends_no_wake(crud_mocks):
    crud_mocks.set_flag.return_value = False
    session = AsyncMock()
    await save(CONTROLLER, _request(_valid_form(bot_kind="legacy")), session, _token())
    assert crud_mocks.set_flag.await_args.kwargs["enabled"] is False
    assert await crud_mocks.redis.xlen(CHANNEL_ENABLED_STREAM) == 0
    session.commit.assert_awaited_once()


async def test_post_still_saves_when_redis_is_down(crud_mocks, monkeypatch):
    """The wake is best-effort: the switch commits and the page saves."""
    down = AsyncMock()
    down.xadd.side_effect = ConnectionError("redis unreachable")
    monkeypatch.setattr(channel_config, "get_redis_client", lambda: down)
    saved: list[str] = []
    monkeypatch.setattr(
        channel_config, "flash_success", lambda request, text: saved.append(text)
    )
    session = AsyncMock()

    response = await save(CONTROLLER, _request(_valid_form()), session, _token())

    assert response.status_code in (302, 303, 307)
    session.commit.assert_awaited_once()
    down.xadd.assert_awaited_once()
    assert saved == ["Channel configuration saved."]
