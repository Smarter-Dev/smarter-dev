"""Tests for the admin ``/chat-bot-settings`` per-channel model-override command.

Covers the pure view/parse helpers, the admin gate on the slash command, and the
select + button + modal-submit interaction handlers (single settings-panel flow).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import hikari
import pytest

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.bot.plugins import model_override
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.models import ChannelModelOverride
from smarter_dev.bot.views.model_override_views import AUTO_TOGGLE_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import CONTINUE_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import FALLBACK_SELECT_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import MAX_TOKEN_BUDGET
from smarter_dev.bot.views.model_override_views import MODAL_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import PanelState
from smarter_dev.bot.views.model_override_views import REASONING_SELECT_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import SENTINEL_DEFAULT
from smarter_dev.bot.views.model_override_views import SENTINEL_MODEL_DEFAULT
from smarter_dev.bot.views.model_override_views import SENTINEL_NO_FALLBACK
from smarter_dev.bot.views.model_override_views import build_fallback_options
from smarter_dev.bot.views.model_override_views import build_model_options
from smarter_dev.bot.views.model_override_views import build_reasoning_options
from smarter_dev.bot.views.model_override_views import create_settings_modal
from smarter_dev.bot.views.model_override_views import create_settings_panel
from smarter_dev.bot.views.model_override_views import encode_panel_state
from smarter_dev.bot.views.model_override_views import parse_budget
from smarter_dev.bot.views.model_override_views import parse_panel_state

ADMIN = hikari.Permissions.ADMINISTRATOR
NONE = hikari.Permissions.NONE

PERMS_TARGET = "lightbulb.utils.permissions_for"

# A model with no reasoning knob (its panel omits the reasoning select) and one
# with a reasoning ladder (its panel shows the reasoning select).
NO_REASONING_KEY = "kimi-k2-6"
REASONING_KEY = "glm-5-2"
# A second reasoning-capable model used as a fallback target in tests.
FALLBACK_KEY = "deepseek-v4"


def _override(
    model_key: str = NO_REASONING_KEY,
    daily: int = 0,
    hourly: int = 0,
    reasoning_level: str | None = None,
    auto_respond: bool = False,
    fallback_model_key: str | None = None,
    response_filter: str | None = None,
):
    return ChannelModelOverride(
        guild_id="G",
        channel_id="C",
        model_key=model_key,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
        reasoning_level=reasoning_level,
        auto_respond=auto_respond,
        fallback_model_key=fallback_model_key,
        response_filter=response_filter,
    )


def _row_components(rows):
    """Flatten action-row builders into their child component builders."""
    return [component for row in rows for component in row.components]


# --------------------------------------------------------------------------- #
# Pure helpers — model options
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


# --------------------------------------------------------------------------- #
# Pure helpers — budget parsing
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Pure helpers — reasoning options
# --------------------------------------------------------------------------- #


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
# Pure helpers — fallback options
# --------------------------------------------------------------------------- #


def test_build_fallback_options_excludes_primary_and_offers_sentinel():
    options = build_fallback_options(primary_key=REASONING_KEY, current_fallback_key=None)
    values = [opt.value for opt in options]

    assert values[0] == SENTINEL_NO_FALLBACK
    # The chosen primary is never offered as its own fallback.
    assert REASONING_KEY not in values
    # Every other catalog model is offered.
    for model in MODEL_CATALOG:
        if model.key != REASONING_KEY:
            assert model.key in values
    # sentinel + (catalog - primary), within Discord's 25-option limit
    assert len(options) == len(MODEL_CATALOG)
    assert len(options) <= 25
    # No stored fallback -> the "no fallback" sentinel is preselected.
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == [SENTINEL_NO_FALLBACK]


def test_build_fallback_options_marks_current_fallback_default():
    options = build_fallback_options(
        primary_key=REASONING_KEY, current_fallback_key=FALLBACK_KEY
    )
    defaults = [opt.value for opt in options if opt.is_default]
    assert defaults == [FALLBACK_KEY]


# --------------------------------------------------------------------------- #
# Pure helpers — panel state encode/parse
# --------------------------------------------------------------------------- #


def test_encode_panel_state_full():
    assert (
        encode_panel_state(REASONING_KEY, "high", True, FALLBACK_KEY)
        == f"{REASONING_KEY}:high:1:{FALLBACK_KEY}"
    )


def test_encode_panel_state_empties():
    assert encode_panel_state(NO_REASONING_KEY, None, False, None) == f"{NO_REASONING_KEY}::0:"


def test_parse_panel_state_roundtrip():
    custom_id = f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}"
    state = parse_panel_state(custom_id, AUTO_TOGGLE_CUSTOM_ID_PREFIX)
    assert state == PanelState(
        model_key=REASONING_KEY,
        reasoning_level="high",
        auto_respond=True,
        fallback_model_key=FALLBACK_KEY,
    )


def test_parse_panel_state_empties():
    custom_id = f"{CONTINUE_CUSTOM_ID_PREFIX}:{NO_REASONING_KEY}::0:"
    state = parse_panel_state(custom_id, CONTINUE_CUSTOM_ID_PREFIX)
    assert state == PanelState(
        model_key=NO_REASONING_KEY,
        reasoning_level=None,
        auto_respond=False,
        fallback_model_key=None,
    )


def test_parse_panel_state_wrong_prefix_returns_none():
    custom_id = f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}"
    assert parse_panel_state(custom_id, CONTINUE_CUSTOM_ID_PREFIX) is None


def test_parse_panel_state_wrong_field_count_returns_none():
    assert parse_panel_state(f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:only:two", AUTO_TOGGLE_CUSTOM_ID_PREFIX) is None


# --------------------------------------------------------------------------- #
# Pure helpers — settings panel
# --------------------------------------------------------------------------- #


def test_create_settings_panel_reasoning_model_has_reasoning_row():
    model = get_model(REASONING_KEY)
    rows = create_settings_panel(model, None, False, None)
    # reasoning select + fallback select + button row
    assert len(rows) == 3
    prefixes = [row.components[0].custom_id.split(":")[0] for row in rows[:2]]
    assert prefixes == [REASONING_SELECT_CUSTOM_ID_PREFIX, FALLBACK_SELECT_CUSTOM_ID_PREFIX]


def test_create_settings_panel_no_reasoning_model_omits_reasoning_row():
    model = get_model(NO_REASONING_KEY)
    rows = create_settings_panel(model, None, False, None)
    # No reasoning knob -> fallback select + button row only.
    assert len(rows) == 2
    assert rows[0].components[0].custom_id.split(":")[0] == FALLBACK_SELECT_CUSTOM_ID_PREFIX


def test_create_settings_panel_buttons_carry_auto_and_continue():
    model = get_model(NO_REASONING_KEY)
    rows = create_settings_panel(model, None, False, None)
    buttons = rows[-1].components
    button_prefixes = [button.custom_id.split(":")[0] for button in buttons]
    assert AUTO_TOGGLE_CUSTOM_ID_PREFIX in button_prefixes
    assert CONTINUE_CUSTOM_ID_PREFIX in button_prefixes


def test_create_settings_panel_auto_button_reflects_off_state():
    model = get_model(NO_REASONING_KEY)
    rows = create_settings_panel(model, None, False, None)
    auto_button = next(
        button
        for button in rows[-1].components
        if button.custom_id.split(":")[0] == AUTO_TOGGLE_CUSTOM_ID_PREFIX
    )
    assert "OFF" in auto_button.label
    assert auto_button.style == hikari.ButtonStyle.SECONDARY


def test_create_settings_panel_auto_button_reflects_on_state():
    model = get_model(NO_REASONING_KEY)
    rows = create_settings_panel(model, None, True, None)
    auto_button = next(
        button
        for button in rows[-1].components
        if button.custom_id.split(":")[0] == AUTO_TOGGLE_CUSTOM_ID_PREFIX
    )
    assert "ON" in auto_button.label
    assert auto_button.style == hikari.ButtonStyle.SUCCESS


def test_create_settings_panel_custom_ids_carry_state_and_stay_short():
    model = get_model(REASONING_KEY)
    rows = create_settings_panel(model, "high", True, FALLBACK_KEY)
    state = encode_panel_state(REASONING_KEY, "high", True, FALLBACK_KEY)
    for component in _row_components(rows):
        assert component.custom_id.endswith(state)
        assert len(component.custom_id) < 100


def test_create_settings_panel_prefills_reasoning_and_fallback():
    model = get_model(REASONING_KEY)
    rows = create_settings_panel(model, "high", False, FALLBACK_KEY)
    reasoning_select = rows[0].components[0]
    fallback_select = rows[1].components[0]
    reasoning_defaults = [o.value for o in reasoning_select.options if o.is_default]
    fallback_defaults = [o.value for o in fallback_select.options if o.is_default]
    assert reasoning_defaults == ["high"]
    assert fallback_defaults == [FALLBACK_KEY]


# --------------------------------------------------------------------------- #
# Pure helpers — settings modal
# --------------------------------------------------------------------------- #


def _panel_state(
    model_key: str = NO_REASONING_KEY,
    reasoning_level: str | None = None,
    auto_respond: bool = False,
    fallback_model_key: str | None = None,
) -> PanelState:
    return PanelState(
        model_key=model_key,
        reasoning_level=reasoning_level,
        auto_respond=auto_respond,
        fallback_model_key=fallback_model_key,
    )


def test_create_settings_modal_encodes_full_state():
    state = _panel_state(REASONING_KEY, "high", True, FALLBACK_KEY)
    modal = create_settings_modal(state, None)
    assert modal.custom_id == (
        f"{MODAL_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}"
    )


def test_create_settings_modal_has_budget_and_filter_inputs():
    modal = create_settings_modal(_panel_state(), None)
    inputs = _row_components(modal.components)
    ids = [component.custom_id for component in inputs]
    assert ids == ["daily_budget", "hourly_budget", "response_filter"]
    response_filter_input = inputs[-1]
    assert response_filter_input.style == hikari.TextInputStyle.PARAGRAPH
    assert response_filter_input.is_required is False
    assert response_filter_input.max_length == 4000


def test_create_settings_modal_omits_prefill_without_override():
    modal = create_settings_modal(_panel_state(), None)
    values = [component.value for component in _row_components(modal.components)]
    assert values == [hikari.UNDEFINED, hikari.UNDEFINED, hikari.UNDEFINED]


def test_create_settings_modal_prefills_from_override():
    modal = create_settings_modal(
        _panel_state(),
        _override(daily=1500, hourly=200, response_filter="only questions"),
    )
    values = [component.value for component in _row_components(modal.components)]
    assert values == ["1500", "200", "only questions"]


def test_create_settings_modal_omits_empty_filter_prefill():
    modal = create_settings_modal(
        _panel_state(), _override(daily=10, hourly=10, response_filter=None)
    )
    values = [component.value for component in _row_components(modal.components)]
    assert values[-1] is hikari.UNDEFINED


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


async def test_chat_bot_settings_denies_non_admin():
    service = AsyncMock()
    ctx = _slash_ctx(NONE, service)
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.chat_bot_settings(ctx)

    ctx.respond.assert_called_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs.get("flags") == hikari.MessageFlag.EPHEMERAL
    assert kwargs.get("components") is None
    service.get_override.assert_not_called()


async def test_chat_bot_settings_shows_select_for_admin():
    service = AsyncMock()
    service.get_override.return_value = None
    ctx = _slash_ctx(ADMIN, service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.chat_bot_settings(ctx)

    service.get_override.assert_awaited_once_with("G", "C")
    ctx.respond.assert_called_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs["components"]  # a select action row was attached
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL


# --------------------------------------------------------------------------- #
# Model select handler
# --------------------------------------------------------------------------- #


def _component_event(
    values: list[str],
    service: AsyncMock,
    custom_id: str = "model_override_select",
):
    interaction = Mock(spec=hikari.ComponentInteraction)
    interaction.member = Mock(spec=hikari.InteractionMember)
    interaction.guild_id = "G"
    interaction.channel_id = "C"
    interaction.values = values
    interaction.custom_id = custom_id
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


async def test_select_sentinel_clear_failure_reports_to_admin():
    """An API failure clearing the override must answer the interaction with a
    friendly error, not escape to the (broken) generic modal error responder."""
    service = AsyncMock()
    service.clear_override.side_effect = APIError("boom", status_code=500)
    event = _component_event([SENTINEL_DEFAULT], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    event.interaction.create_initial_response.assert_awaited_once()
    _, kwargs = event.interaction.create_initial_response.call_args
    assert "Couldn't remove the override" in kwargs["content"]


async def test_select_no_reasoning_model_renders_panel():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event([NO_REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    # The panel is a message update, not a modal — budgets are deferred to the
    # "Budgets & filter…" button.
    event.interaction.create_modal_response.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()
    args, kwargs = event.interaction.create_initial_response.call_args
    assert args[0] == hikari.ResponseType.MESSAGE_UPDATE
    assert kwargs["components"]
    service.clear_override.assert_not_called()


async def test_select_reasoning_model_renders_panel_with_reasoning_row():
    service = AsyncMock()
    service.get_override.return_value = None
    event = _component_event([REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    event.interaction.create_modal_response.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()
    _, kwargs = event.interaction.create_initial_response.call_args
    rows = kwargs["components"]
    # reasoning select + fallback select + buttons
    assert len(rows) == 3


async def test_select_panel_prefills_from_current_override():
    service = AsyncMock()
    service.get_override.return_value = _override(
        model_key=REASONING_KEY,
        reasoning_level="high",
        auto_respond=True,
        fallback_model_key=FALLBACK_KEY,
    )
    event = _component_event([REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_select(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    rows = kwargs["components"]
    state = encode_panel_state(REASONING_KEY, "high", True, FALLBACK_KEY)
    assert rows[0].components[0].custom_id.endswith(state)


async def test_select_denies_non_admin():
    service = AsyncMock()
    event = _component_event([NO_REASONING_KEY], service)
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.handle_model_override_select(event)

    event.interaction.create_initial_response.assert_awaited_once()
    event.interaction.create_modal_response.assert_not_called()
    service.clear_override.assert_not_called()


# --------------------------------------------------------------------------- #
# Panel component handlers (re-render)
# --------------------------------------------------------------------------- #


async def test_reasoning_select_rerenders_panel_with_new_level():
    service = AsyncMock()
    event = _component_event(
        ["high"],
        service,
        custom_id=f"{REASONING_SELECT_CUSTOM_ID_PREFIX}:{REASONING_KEY}::0:",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_reasoning_select(event)

    event.interaction.create_modal_response.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()
    args, kwargs = event.interaction.create_initial_response.call_args
    assert args[0] == hikari.ResponseType.MESSAGE_UPDATE
    state = encode_panel_state(REASONING_KEY, "high", False, None)
    assert kwargs["components"][0].components[0].custom_id.endswith(state)


async def test_reasoning_select_sentinel_maps_to_model_default():
    service = AsyncMock()
    event = _component_event(
        [SENTINEL_MODEL_DEFAULT],
        service,
        custom_id=f"{REASONING_SELECT_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:0:",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_reasoning_select(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    # Sentinel -> empty reasoning segment (the model default).
    state = encode_panel_state(REASONING_KEY, None, False, None)
    assert kwargs["components"][0].components[0].custom_id.endswith(state)


async def test_fallback_select_rerenders_with_new_fallback():
    service = AsyncMock()
    event = _component_event(
        [FALLBACK_KEY],
        service,
        custom_id=f"{FALLBACK_SELECT_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:0:",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_fallback_select(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    state = encode_panel_state(REASONING_KEY, "high", False, FALLBACK_KEY)
    assert kwargs["components"][0].components[0].custom_id.endswith(state)


async def test_fallback_select_sentinel_clears_fallback():
    service = AsyncMock()
    event = _component_event(
        [SENTINEL_NO_FALLBACK],
        service,
        custom_id=f"{FALLBACK_SELECT_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:0:{FALLBACK_KEY}",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_fallback_select(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    state = encode_panel_state(REASONING_KEY, "high", False, None)
    assert kwargs["components"][0].components[0].custom_id.endswith(state)


async def test_auto_toggle_flips_state_off_to_on():
    service = AsyncMock()
    event = _component_event(
        [],
        service,
        custom_id=f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:0:{FALLBACK_KEY}",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_auto_toggle(event)

    args, kwargs = event.interaction.create_initial_response.call_args
    assert args[0] == hikari.ResponseType.MESSAGE_UPDATE
    state = encode_panel_state(REASONING_KEY, "high", True, FALLBACK_KEY)
    for component in _row_components(kwargs["components"]):
        assert component.custom_id.endswith(state)


async def test_auto_toggle_flips_state_on_to_off():
    service = AsyncMock()
    event = _component_event(
        [],
        service,
        custom_id=f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_auto_toggle(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    state = encode_panel_state(REASONING_KEY, "high", False, FALLBACK_KEY)
    assert kwargs["components"][0].components[0].custom_id.endswith(state)


async def test_auto_toggle_denies_non_admin():
    service = AsyncMock()
    event = _component_event(
        [],
        service,
        custom_id=f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:0:",
    )
    with patch(PERMS_TARGET, return_value=NONE):
        await model_override.handle_model_override_auto_toggle(event)

    event.interaction.create_initial_response.assert_awaited_once()
    _, kwargs = event.interaction.create_initial_response.call_args
    assert "Administrator" in kwargs["content"]


async def test_continue_opens_modal_carrying_state():
    service = AsyncMock()
    service.get_override.return_value = _override(daily=1500, hourly=200)
    event = _component_event(
        [],
        service,
        custom_id=f"{CONTINUE_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}",
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_continue(event)

    event.interaction.create_modal_response.assert_awaited_once()
    args, _ = event.interaction.create_modal_response.call_args
    assert args[1] == f"{MODAL_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}"


# --------------------------------------------------------------------------- #
# Modal submit handler
# --------------------------------------------------------------------------- #


def _text_input(custom_id: str, value: str):
    component = Mock()
    component.custom_id = custom_id
    component.value = value
    return component


def _modal_event(custom_id: str, fields: dict[str, str], service: AsyncMock):
    interaction = Mock(spec=hikari.ModalInteraction)
    interaction.member = Mock(spec=hikari.InteractionMember)
    interaction.guild_id = "G"
    interaction.channel_id = "C"
    interaction.custom_id = custom_id
    interaction.components = [
        [_text_input("daily_budget", fields.get("daily_budget", ""))],
        [_text_input("hourly_budget", fields.get("hourly_budget", ""))],
        [_text_input("response_filter", fields.get("response_filter", ""))],
    ]
    interaction.create_initial_response = AsyncMock()
    event = Mock()
    event.interaction = interaction
    event.app = Mock()
    event.app.d = {"model_override_service": service}
    return event


async def test_modal_submit_persists_all_fields():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}",
        {
            "daily_budget": "1500",
            "hourly_budget": "200",
            "response_filter": "Only respond to programming questions",
        },
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_awaited_once_with(
        "G",
        "C",
        REASONING_KEY,
        1500,
        200,
        reasoning_level="high",
        auto_respond=True,
        fallback_model_key=FALLBACK_KEY,
        response_filter="Only respond to programming questions",
    )
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_persists_minimal_state():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{NO_REASONING_KEY}::0:",
        {"daily_budget": "0", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_awaited_once_with(
        "G",
        "C",
        NO_REASONING_KEY,
        0,
        0,
        reasoning_level=None,
        auto_respond=False,
        fallback_model_key=None,
        response_filter=None,
    )


async def test_modal_submit_empty_filter_persists_none():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}",
        {"daily_budget": "0", "hourly_budget": "0", "response_filter": "   "},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    _, kwargs = service.set_override.call_args
    assert kwargs["response_filter"] is None


async def test_modal_submit_confirmation_lists_all_settings():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{REASONING_KEY}:high:1:{FALLBACK_KEY}",
        {
            "daily_budget": "1500",
            "hourly_budget": "200",
            "response_filter": "Only respond to programming questions",
        },
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    content = kwargs["content"]
    assert "Auto-respond" in content
    assert "on" in content.lower()
    assert get_model(FALLBACK_KEY).label in content
    # A configured filter is reported as present.
    assert "filter" in content.lower()


async def test_modal_submit_confirmation_reports_no_fallback_and_no_filter():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{NO_REASONING_KEY}::0:",
        {"daily_budget": "0", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    content = kwargs["content"]
    assert "none" in content.lower()


async def test_modal_submit_save_failure_reports_to_admin():
    """An API failure saving the override must answer the interaction with a
    friendly error, not escape to the (broken) generic modal error responder."""
    service = AsyncMock()
    service.set_override.side_effect = APIError("boom", status_code=500)
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{NO_REASONING_KEY}::0:",
        {"daily_budget": "1500", "hourly_budget": "0"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    event.interaction.create_initial_response.assert_awaited_once()
    _, kwargs = event.interaction.create_initial_response.call_args
    assert "Couldn't save the override" in kwargs["content"]


async def test_modal_submit_rejects_invalid_budget():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:{NO_REASONING_KEY}::0:",
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
        f"{MODAL_CUSTOM_ID_PREFIX}:{NO_REASONING_KEY}::0:",
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
        f"{MODAL_CUSTOM_ID_PREFIX}:not-a-real-model::0:",
        {"daily_budget": "10", "hourly_budget": "10"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()


async def test_modal_submit_rejects_malformed_custom_id():
    service = AsyncMock()
    event = _modal_event(
        f"{MODAL_CUSTOM_ID_PREFIX}:too:few",
        {"daily_budget": "10", "hourly_budget": "10"},
        service,
    )
    with patch(PERMS_TARGET, return_value=ADMIN):
        await model_override.handle_model_override_modal_submit(event)

    service.set_override.assert_not_called()
    event.interaction.create_initial_response.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Budget-exhausted fallback button handler (member-facing, no admin gate)
# --------------------------------------------------------------------------- #

REGISTRY_TARGET = "smarter_dev.bot.plugins.model_override.get_chat_engine_registry"


def _fallback_button_event(
    service: AsyncMock,
    redis,
    *,
    channel_id: str = "123",
    guild_id: str = "456",
    reset_epoch: int | None = None,
):
    if reset_epoch is None:
        reset_epoch = int(datetime.now(UTC).timestamp()) + 3600
    interaction = Mock(spec=hikari.ComponentInteraction)
    interaction.guild_id = guild_id
    interaction.channel_id = channel_id
    interaction.custom_id = f"model_budget_fallback:{reset_epoch}"
    interaction.create_initial_response = AsyncMock()
    event = Mock()
    event.interaction = interaction
    event.app = Mock()
    data = {"model_override_service": service}
    if redis is not None:
        data["chat_memory_redis"] = redis
    event.app.d = data
    return event


def _stub_registry(engine=None):
    registry = AsyncMock()
    registry.get.return_value = engine
    return registry


async def test_fallback_button_sets_keys_updates_message_and_fires_engine():
    service = AsyncMock()
    service.get_override.return_value = _override(fallback_model_key=FALLBACK_KEY)
    redis = AsyncMock()
    engine = Mock()
    engine.active = True
    engine.fire_now = Mock()
    reset_epoch = int(datetime.now(UTC).timestamp()) + 3600
    event = _fallback_button_event(service, redis, reset_epoch=reset_epoch)

    with patch(REGISTRY_TARGET, return_value=_stub_registry(engine)):
        await model_override.handle_model_budget_fallback(event)

    assert redis.set.await_count == 2
    keys = [call.args[0] for call in redis.set.await_args_list]
    assert keys == [
        "modelbudget-fallback:123",
        "modelbudget-fallback-ended:123",
    ]
    exats = [call.kwargs["exat"] for call in redis.set.await_args_list]
    assert exats == [reset_epoch, reset_epoch + 86400]

    args, kwargs = event.interaction.create_initial_response.call_args
    assert args[0] == hikari.ResponseType.MESSAGE_UPDATE
    assert kwargs["components"] == []
    assert get_model(FALLBACK_KEY).label in kwargs["content"]
    engine.fire_now.assert_called_once()


async def test_fallback_button_no_fallback_configured_reports_ephemeral():
    service = AsyncMock()
    service.get_override.return_value = _override(fallback_model_key=None)
    redis = AsyncMock()
    event = _fallback_button_event(service, redis)

    with patch(REGISTRY_TARGET, return_value=_stub_registry()):
        await model_override.handle_model_budget_fallback(event)

    redis.set.assert_not_called()
    args, kwargs = event.interaction.create_initial_response.call_args
    assert args[0] == hikari.ResponseType.MESSAGE_CREATE
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "no longer has a fallback" in kwargs["content"].lower()


async def test_fallback_button_past_epoch_says_already_reset():
    service = AsyncMock()
    service.get_override.return_value = _override(fallback_model_key=FALLBACK_KEY)
    redis = AsyncMock()
    past = int(datetime.now(UTC).timestamp()) - 10
    event = _fallback_button_event(service, redis, reset_epoch=past)

    with patch(REGISTRY_TARGET, return_value=_stub_registry()):
        await model_override.handle_model_budget_fallback(event)

    redis.set.assert_not_called()
    _, kwargs = event.interaction.create_initial_response.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "already reset" in kwargs["content"].lower()


async def test_fallback_button_redis_unavailable_reports_ephemeral():
    service = AsyncMock()
    service.get_override.return_value = _override(fallback_model_key=FALLBACK_KEY)
    event = _fallback_button_event(service, None)  # no chat_memory_redis on bot.d

    with patch(REGISTRY_TARGET, return_value=_stub_registry()):
        await model_override.handle_model_budget_fallback(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "couldn't switch" in kwargs["content"].lower()


async def test_fallback_button_redis_error_reports_ephemeral():
    service = AsyncMock()
    service.get_override.return_value = _override(fallback_model_key=FALLBACK_KEY)
    redis = AsyncMock()
    redis.set.side_effect = Exception("redis down")
    event = _fallback_button_event(service, redis)

    with patch(REGISTRY_TARGET, return_value=_stub_registry()):
        await model_override.handle_model_budget_fallback(event)

    _, kwargs = event.interaction.create_initial_response.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert "couldn't switch" in kwargs["content"].lower()
