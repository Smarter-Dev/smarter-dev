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

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.bot.plugins import model_override
from smarter_dev.bot.services.models import ChannelModelOverride
from smarter_dev.bot.views.model_override_views import SENTINEL_DEFAULT
from smarter_dev.bot.views.model_override_views import SENTINEL_MODEL_DEFAULT
from smarter_dev.bot.views.model_override_views import MAX_TOKEN_BUDGET
from smarter_dev.bot.views.model_override_views import build_model_options
from smarter_dev.bot.views.model_override_views import build_reasoning_options
from smarter_dev.bot.views.model_override_views import create_budgets_modal
from smarter_dev.bot.views.model_override_views import parse_budget

ADMIN = hikari.Permissions.ADMINISTRATOR
NONE = hikari.Permissions.NONE

PERMS_TARGET = "lightbulb.utils.permissions_for"

# A model with no reasoning knob (goes straight to the budgets modal) and one
# with a reasoning ladder (gets the intermediate reasoning select).
NO_REASONING_KEY = "kimi-k2-6"
REASONING_KEY = "glm-5-2"


def _override(
    model_key: str = NO_REASONING_KEY,
    daily: int = 0,
    hourly: int = 0,
    reasoning_level: str | None = None,
):
    return ChannelModelOverride(
        guild_id="G",
        channel_id="C",
        model_key=model_key,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
        reasoning_level=reasoning_level,
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
    options = build_model_options(current_key=REASONING_KEY)
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == [REASONING_KEY]


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


def test_parse_budget_accepts_maximum():
    assert parse_budget(str(MAX_TOKEN_BUDGET)) == MAX_TOKEN_BUDGET


def test_parse_budget_rejects_over_limit():
    with pytest.raises(ValueError, match="too large"):
        parse_budget(str(MAX_TOKEN_BUDGET + 1))


def test_create_budgets_modal_omits_value_without_override():
    # A first-time-setup modal (no existing override) must leave the budget
    # inputs' value UNDEFINED — an empty string is rejected by Discord.
    modal = create_budgets_modal("G", "C", NO_REASONING_KEY, None, None)
    values = [
        component.value
        for row in modal.components
        for component in row.components
    ]
    assert values == [hikari.UNDEFINED, hikari.UNDEFINED]


def test_create_budgets_modal_inputs_cap_length_at_ten():
    modal = create_budgets_modal("G", "C", NO_REASONING_KEY, None, None)
    lengths = [
        component.max_length
        for row in modal.components
        for component in row.components
    ]
    assert lengths == [10, 10]


def test_create_budgets_modal_encodes_ids_and_prefills():
    modal = create_budgets_modal(
        "G", "C", NO_REASONING_KEY, None, _override(daily=1500, hourly=200)
    )
    # Trailing empty segment encodes "no explicit reasoning level".
    assert modal.custom_id == f"model_override_modal:G:C:{NO_REASONING_KEY}:"
    values = [
        component.value
        for row in modal.components
        for component in row.components
    ]
    assert "1500" in values
    assert "200" in values


def test_create_budgets_modal_encodes_reasoning_level():
    modal = create_budgets_modal("G", "C", REASONING_KEY, "high", _override())
    assert modal.custom_id == f"model_override_modal:G:C:{REASONING_KEY}:high"


def test_build_reasoning_options_covers_model_levels_and_sentinel():
    model = get_model(REASONING_KEY)
    options = build_reasoning_options(model, current_level=None)
    values = [opt.value for opt in options]
    assert values[0] == SENTINEL_MODEL_DEFAULT
    for level in model.reasoning_levels:
        assert level.value in values
    # No stored choice -> the model-default sentinel is preselected.
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == [SENTINEL_MODEL_DEFAULT]


def test_build_reasoning_options_marks_stored_level_default():
    model = get_model(REASONING_KEY)
    options = build_reasoning_options(model, current_level="high")
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == ["high"]


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


async def test_select_no_reasoning_model_opens_budget_modal():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event([NO_REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    event.interaction.create_modal_response.assert_awaited_once()
    args, kwargs = event.interaction.create_modal_response.call_args
    assert args[1] == f"model_override_modal:G:C:{NO_REASONING_KEY}:"
    service.clear_override.assert_not_called()


async def test_select_reasoning_model_shows_reasoning_select():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event([REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    # A reasoning-capable model updates the message with the reasoning select
    # rather than jumping straight to the budgets modal.
    event.interaction.create_modal_response.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()
    _, kwargs = event.interaction.create_initial_response.call_args
    assert kwargs["components"]


async def test_select_denies_non_admin():
    service = AsyncMock()
    event = _component_event([NO_REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.handle_model_override_select(event)

    event.interaction.create_initial_response.assert_awaited_once()
    event.interaction.create_modal_response.assert_not_called()
    service.clear_override.assert_not_called()


async def test_reasoning_select_opens_budget_modal_with_level():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event(["high"], service)
    event.interaction.custom_id = (
        f"model_override_reasoning:G:C:{REASONING_KEY}"
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_reasoning_select(event)

    event.interaction.create_modal_response.assert_awaited_once()
    args, _ = event.interaction.create_modal_response.call_args
    assert args[1] == f"model_override_modal:G:C:{REASONING_KEY}:high"


async def test_reasoning_select_sentinel_maps_to_model_default():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event([SENTINEL_MODEL_DEFAULT], service)
    event.interaction.custom_id = (
        f"model_override_reasoning:G:C:{REASONING_KEY}"
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_reasoning_select(event)

    event.interaction.create_modal_response.assert_awaited_once()
    args, _ = event.interaction.create_modal_response.call_args
    # Empty reasoning segment -> stored as the model default (NULL).
    assert args[1] == f"model_override_modal:G:C:{REASONING_KEY}:"


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


async def test_modal_submit_persists_override_with_model_default_reasoning():
    service = AsyncMock()
    event = _modal_event(
        f"model_override_modal:G:C:{NO_REASONING_KEY}:",
        {"daily_budget": "1500", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    # Empty reasoning segment persists as None (use the model default).
    service.set_override.assert_awaited_once_with(
        "G", "C", NO_REASONING_KEY, 1500, 0, reasoning_level=None
    )
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_persists_selected_reasoning_level():
    service = AsyncMock()
    event = _modal_event(
        f"model_override_modal:G:C:{REASONING_KEY}:high",
        {"daily_budget": "0", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_awaited_once_with(
        "G", "C", REASONING_KEY, 0, 0, reasoning_level="high"
    )
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_rejects_invalid_budget():
    service = AsyncMock()
    event = _modal_event(
        f"model_override_modal:G:C:{NO_REASONING_KEY}:",
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
        f"model_override_modal:G:C:{NO_REASONING_KEY}:",
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
        "model_override_modal:G:C:not-a-real-model:",
        {"daily_budget": "10", "hourly_budget": "10"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()
