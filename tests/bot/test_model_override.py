"""Tests for the admin ``/setmodel`` per-channel model-override command.

Covers the pure view/parse helpers, the admin gate on the slash command, and the
select + modal-submit interaction handlers (Path B, two-step flow).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import hikari
import pytest

from smarter_dev.bot.agents.model_catalog import MODEL_CATALOG
from smarter_dev.bot.plugins import model_override
from smarter_dev.bot.services.models import ChannelModelOverride
from smarter_dev.bot.views.model_override_views import SENTINEL_DEFAULT
from smarter_dev.bot.views.model_override_views import build_model_options
from smarter_dev.bot.views.model_override_views import create_budgets_modal
from smarter_dev.bot.views.model_override_views import parse_budget

ADMIN = hikari.Permissions.ADMINISTRATOR
NONE = hikari.Permissions.NONE

PERMS_TARGET = "lightbulb.utils.permissions_for"


def _override(model_key: str = "kimi-k2", daily: int = 0, hourly: int = 0):
    return ChannelModelOverride(
        guild_id="G",
        channel_id="C",
        model_key=model_key,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_build_model_options_covers_catalog_and_sentinel():
    options = build_model_options(current_key=None)
    values = [opt.value for opt in options]

    assert values[0] == SENTINEL_DEFAULT
    for model in MODEL_CATALOG:
        assert model.key in values
    # sentinel + every catalog model, and within Discord's 25-option limit
    assert len(options) == len(MODEL_CATALOG) + 1
    assert len(options) <= 25


def test_build_model_options_marks_current_key_default():
    options = build_model_options(current_key="glm-4-6")
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == ["glm-4-6"]


def test_build_model_options_defaults_to_sentinel_without_override():
    options = build_model_options(current_key=None)
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == [SENTINEL_DEFAULT]


@pytest.mark.parametrize(
    "raw,expected",
    [("", 0), (None, 0), ("   ", 0), ("0", 0), ("1500", 1500), ("42", 42)],
)
def test_parse_budget_valid(raw, expected):
    assert parse_budget(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5", "1,000", "1e3"])
def test_parse_budget_invalid(raw):
    with pytest.raises(ValueError):
        parse_budget(raw)


def test_create_budgets_modal_encodes_ids_and_prefills():
    modal = create_budgets_modal("G", "C", "kimi-k2", _override(daily=1500, hourly=200))
    assert modal.custom_id == "model_override_modal:G:C:kimi-k2"
    values = [
        component.value
        for row in modal.components
        for component in row.components
    ]
    assert "1500" in values
    assert "200" in values


# --------------------------------------------------------------------------- #
# Slash command admin gate
# --------------------------------------------------------------------------- #


def _slash_ctx(member_perms: hikari.Permissions, service: AsyncMock):
    ctx = Mock()
    ctx.member = Mock(spec=hikari.InteractionMember)
    ctx.guild_id = "G"
    ctx.channel_id = "C"
    ctx.respond = AsyncMock()
    ctx.bot = Mock()
    ctx.bot.d = {"model_override_service": service}
    return ctx


async def test_setmodel_denies_non_admin():
    service = AsyncMock()
    ctx = _slash_ctx(NONE, service)
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.setmodel(ctx)

    ctx.respond.assert_called_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs.get("flags") == hikari.MessageFlag.EPHEMERAL
    assert kwargs.get("components") is None
    service.get_override.assert_not_called()


async def test_setmodel_shows_select_for_admin():
    service = AsyncMock()
    service.get_override.return_value = None
    ctx = _slash_ctx(ADMIN, service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.setmodel(ctx)

    service.get_override.assert_awaited_once_with("G", "C")
    ctx.respond.assert_called_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs["components"]  # a select action row was attached
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL


# --------------------------------------------------------------------------- #
# Select handler
# --------------------------------------------------------------------------- #


def _component_event(values: list[str], service: AsyncMock, admin: bool = True):
    interaction = Mock(spec=hikari.ComponentInteraction)
    interaction.member = Mock(spec=hikari.InteractionMember)
    interaction.guild_id = "G"
    interaction.channel_id = "C"
    interaction.values = values
    interaction.create_initial_response = AsyncMock()
    interaction.create_modal_response = AsyncMock()
    event = Mock()
    event.interaction = interaction
    event.app = Mock()
    event.app.d = {"model_override_service": service}
    return event


async def test_select_sentinel_clears_override():
    service = AsyncMock()
    event = _component_event([SENTINEL_DEFAULT], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    service.clear_override.assert_awaited_once_with("G", "C")
    event.interaction.create_initial_response.assert_awaited_once()
    event.interaction.create_modal_response.assert_not_called()


async def test_select_model_opens_budget_modal():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event(["kimi-k2"], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    event.interaction.create_modal_response.assert_awaited_once()
    args, kwargs = event.interaction.create_modal_response.call_args
    assert args[1] == "model_override_modal:G:C:kimi-k2"
    service.clear_override.assert_not_called()


async def test_select_denies_non_admin():
    service = AsyncMock()
    event = _component_event(["kimi-k2"], service)
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.handle_model_override_select(event)

    event.interaction.create_initial_response.assert_awaited_once()
    event.interaction.create_modal_response.assert_not_called()
    service.clear_override.assert_not_called()


# --------------------------------------------------------------------------- #
# Modal submit handler
# --------------------------------------------------------------------------- #


def _text_input(custom_id: str, value: str):
    component = Mock()
    component.custom_id = custom_id
    component.value = value
    return component


def _modal_event(custom_id: str, budgets: dict[str, str], service: AsyncMock):
    interaction = Mock(spec=hikari.ModalInteraction)
    interaction.member = Mock(spec=hikari.InteractionMember)
    interaction.custom_id = custom_id
    interaction.components = [
        [_text_input("daily_budget", budgets.get("daily_budget", ""))],
        [_text_input("hourly_budget", budgets.get("hourly_budget", ""))],
    ]
    interaction.create_initial_response = AsyncMock()
    event = Mock()
    event.interaction = interaction
    event.app = Mock()
    event.app.d = {"model_override_service": service}
    return event


async def test_modal_submit_persists_override():
    service = AsyncMock()
    event = _modal_event(
        "model_override_modal:G:C:kimi-k2",
        {"daily_budget": "1500", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_awaited_once_with("G", "C", "kimi-k2", 1500, 0)
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_rejects_invalid_budget():
    service = AsyncMock()
    event = _modal_event(
        "model_override_modal:G:C:kimi-k2",
        {"daily_budget": "-5", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_denies_non_admin():
    service = AsyncMock()
    event = _modal_event(
        "model_override_modal:G:C:kimi-k2",
        {"daily_budget": "10", "hourly_budget": "10"},
        service,
    )
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_rejects_unknown_model():
    service = AsyncMock()
    event = _modal_event(
        "model_override_modal:G:C:not-a-real-model",
        {"daily_budget": "10", "hourly_budget": "10"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()
