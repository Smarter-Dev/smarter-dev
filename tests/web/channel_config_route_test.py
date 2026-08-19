"""Route-level tests for the signed-link channel configuration page.

The link is the only authorization, so these assert the security boundary:
a bad, expired or cross-channel link gets nothing, and a good one writes
exactly the channel it names.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from smarter_dev.shared import config_links
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
    with (
        patch.object(
            channel_config, "get_channel_model_override", new=AsyncMock(return_value=None)
        ) as get_override,
        patch.object(
            channel_config,
            "get_proactive_channel_settings",
            new=AsyncMock(return_value=None),
        ) as get_proactive,
        patch.object(
            channel_config, "upsert_channel_model_override", new=AsyncMock()
        ) as upsert_override,
        patch.object(
            channel_config,
            "upsert_proactive_channel_settings",
            new=AsyncMock(),
        ) as upsert_proactive,
        patch.object(channel_config, "flash_success", lambda *a, **k: None),
        patch.object(channel_config, "flash_error", lambda *a, **k: None),
        patch.object(channel_config, "get_flash_messages", lambda request: []),
    ):
        yield SimpleNamespace(
            get_override=get_override,
            get_proactive=get_proactive,
            upsert_override=upsert_override,
            upsert_proactive=upsert_proactive,
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
    proactive_kwargs = crud_mocks.upsert_proactive.await_args.kwargs
    assert proactive_kwargs["enabled"] is True
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
    crud_mocks.upsert_proactive.assert_not_awaited()


async def test_post_rejects_an_invalid_form_without_writing(crud_mocks):
    response = await save(CONTROLLER,
        _request(_valid_form(daily_token_budget="-5")), AsyncMock(), _token()
    )
    assert response.status_code in (302, 303, 307)
    crud_mocks.upsert_override.assert_not_awaited()


async def test_post_preserves_agent_written_watch_instructions(crud_mocks):
    crud_mocks.get_proactive.return_value = SimpleNamespace(
        watch_addendum='[{"id": "w1", "text": "watch benchmarks"}]'
    )
    await save(CONTROLLER, _request(_valid_form()), AsyncMock(), _token())
    kwargs = crud_mocks.upsert_proactive.await_args.kwargs
    assert kwargs["watch_addendum"] == '[{"id": "w1", "text": "watch benchmarks"}]'
