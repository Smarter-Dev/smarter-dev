"""Discord components for the admin per-channel chat-bot-settings command.

Single-panel flow — chosen because the installed hikari (2.1.1) exposes no
``Label`` component builder, so Discord modal string-selects are unsupported and
a Discord message holds at most five action rows. The model is picked in an
initial message select; everything else lives in one follow-up *settings panel*
(selects + buttons cannot share a modal), and only the free-text budgets and
response filter go in a modal:

1. ``/chat-bot-settings`` responds (ephemeral) with a message string-select of
   models (``create_model_select_message``), plus a "server default" sentinel.
2. Picking a model swaps the message for the settings panel
   (``create_settings_panel``): an optional reasoning select (reasoning-capable
   models only), a fallback-model select, and a button row with an auto-respond
   toggle and a "Budgets & filter…" button. The panel's state (chosen model,
   reasoning level, auto flag, fallback key) rides in every component's
   ``custom_id`` — see :func:`encode_panel_state` / :func:`parse_panel_state` —
   so each select/toggle re-renders the panel with the updated state and no
   server-side session. Picking the server-default sentinel clears the override
   without a panel.
3. The "Budgets & filter…" button opens the settings modal
   (``create_settings_modal``) whose ``custom_id`` carries the full panel state;
   its text inputs collect the daily/hourly budgets and the response filter.

Everything here is a pure builder or parser — the actual API writes and Discord
responses live in :mod:`smarter_dev.bot.plugins.model_override`.
"""

from __future__ import annotations

from dataclasses import dataclass

import hikari

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import models_by_family
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.bot.services.models import ChannelModelOverride

# Model-select value that means "remove any override and fall back to the server
# default model". Kept distinct from every catalog key.
SENTINEL_DEFAULT = "__default__"

# Reasoning-select value that means "use the model's own default level" (stored
# as NULL). Kept distinct from every ReasoningLevel value.
SENTINEL_MODEL_DEFAULT = "__model_default__"

# Fallback-select value that means "no fallback model" (stored as NULL). Kept
# distinct from every catalog key.
SENTINEL_NO_FALLBACK = "__no_fallback__"

# custom_id constant/prefixes routed in smarter_dev.bot.plugins.events. The
# panel prefixes are deliberately short (``cbs_`` = chat-bot-settings) so the
# encoded state still fits inside Discord's 100-char custom_id limit.
SELECT_CUSTOM_ID = "model_override_select"
REASONING_SELECT_CUSTOM_ID_PREFIX = "cbs_reasoning"
FALLBACK_SELECT_CUSTOM_ID_PREFIX = "cbs_fallback"
AUTO_TOGGLE_CUSTOM_ID_PREFIX = "cbs_auto"
CONTINUE_CUSTOM_ID_PREFIX = "cbs_continue"
MODAL_CUSTOM_ID_PREFIX = "cbs_modal"

# Prefix for the button the chat engine attaches to a budget-exhausted notice,
# offering the channel's configured fallback model. Its only state is the budget
# reset epoch (``model_budget_fallback:<reset_epoch>``); channel/guild come from
# the interaction. Routed in smarter_dev.bot.plugins.events like the others and
# kept well under Discord's 100-char custom_id limit.
MODEL_BUDGET_FALLBACK_CUSTOM_ID_PREFIX = "model_budget_fallback"

# Server-side cap on a token budget (matches the API/DB 32-bit signed maximum).
# Enforced here too so an over-limit entry gets a clear message instead of
# Discord's generic "This interaction failed".
MAX_TOKEN_BUDGET = 2_147_483_647


@dataclass(frozen=True)
class PanelState:
    """The settings-panel state threaded through every component ``custom_id``.

    Attributes:
        model_key: The chosen primary model's catalog key.
        reasoning_level: The chosen reasoning value, or ``None`` for the model
            default.
        auto_respond: Whether the bot replies to any message (not just mentions).
        fallback_model_key: The chosen fallback model's key, or ``None`` for none.
    """

    model_key: str
    reasoning_level: str | None
    auto_respond: bool
    fallback_model_key: str | None


def encode_panel_state(
    model_key: str,
    reasoning_level: str | None,
    auto_respond: bool,
    fallback_model_key: str | None,
) -> str:
    """Encode the panel state as a colon-joined ``custom_id`` suffix.

    Empty reasoning/fallback segments mean "model default" / "no fallback"; the
    auto flag is ``1``/``0``. Guild and channel are deliberately *not* encoded —
    the panel is ephemeral in the configured channel, so handlers read
    ``interaction.guild_id`` / ``interaction.channel_id`` directly.
    """
    return ":".join(
        (
            model_key,
            reasoning_level or "",
            "1" if auto_respond else "0",
            fallback_model_key or "",
        )
    )


def parse_panel_state(custom_id: str, expected_prefix: str) -> PanelState | None:
    """Parse a panel ``custom_id`` into a :class:`PanelState`.

    Returns ``None`` when the prefix does not match or the field count is wrong
    (a malformed/replayed id), so the caller can reject it cleanly.
    """
    parts = custom_id.split(":")
    if len(parts) != 5 or parts[0] != expected_prefix:
        return None
    _, model_key, reasoning, auto, fallback = parts
    return PanelState(
        model_key=model_key,
        reasoning_level=reasoning or None,
        auto_respond=auto == "1",
        fallback_model_key=fallback or None,
    )


def build_model_options(current_key: str | None) -> list[hikari.impl.SelectOptionBuilder]:
    """Build the string-select options: the server-default sentinel first, then
    every catalog model grouped by family.

    ``current_key`` (the channel's currently-stored model, or ``None`` when no
    override is set) is marked as the pre-selected default. Kept well within
    Discord's 25-option limit by the catalog being <= 24 entries.
    """
    options: list[hikari.impl.SelectOptionBuilder] = [
        hikari.impl.SelectOptionBuilder(
            label="Server default (remove override)",
            value=SENTINEL_DEFAULT,
            description="Use the server's default model for this channel",
            is_default=current_key is None,
        )
    ]
    for family, models in models_by_family().items():
        for model in models:
            options.append(
                hikari.impl.SelectOptionBuilder(
                    label=f"{family} · {model.label}",
                    value=model.key,
                    is_default=model.key == current_key,
                )
            )
    return options


def create_model_select_message(
    current: ChannelModelOverride | None,
) -> list[hikari.impl.MessageActionRowBuilder]:
    """Build the ephemeral message components: one action row holding the model
    string-select, pre-selecting the channel's current override if any."""
    current_key = current.model_key if current is not None else None
    action_row = hikari.impl.MessageActionRowBuilder()
    menu = action_row.add_text_menu(
        SELECT_CUSTOM_ID,
        placeholder="Choose a model for this channel",
        min_values=1,
        max_values=1,
    )
    for option in build_model_options(current_key):
        menu.add_option(
            option.label,
            option.value,
            description=option.description,
            is_default=option.is_default,
        )
    return [action_row]


def build_reasoning_options(
    model: CatalogModel, current_level: str | None
) -> list[hikari.impl.SelectOptionBuilder]:
    """Build the reasoning-select options for ``model``: a "model default"
    sentinel first, then each level the model supports (ascending effort).

    ``current_level`` (the channel's stored reasoning value, or ``None``) marks
    the pre-selected default; an unknown/unsupported stored value falls back to
    the sentinel being selected.
    """
    selected = parse_reasoning_level(current_level)
    selected_is_supported = selected in model.reasoning_levels
    options: list[hikari.impl.SelectOptionBuilder] = [
        hikari.impl.SelectOptionBuilder(
            label=f"Model default ({model.default_reasoning.label})"
            if model.default_reasoning is not None
            else "Model default",
            value=SENTINEL_MODEL_DEFAULT,
            description="Use this model's default reasoning level",
            is_default=not selected_is_supported,
        )
    ]
    for level in model.reasoning_levels:
        options.append(
            hikari.impl.SelectOptionBuilder(
                label=level.label,
                value=level.value,
                is_default=selected_is_supported and level == selected,
            )
        )
    return options


def build_fallback_options(
    primary_key: str, current_fallback_key: str | None
) -> list[hikari.impl.SelectOptionBuilder]:
    """Build the fallback-select options: a "no fallback" sentinel first, then
    every catalog model except the chosen ``primary_key`` (a model never falls
    back to itself), grouped by family.

    ``current_fallback_key`` (the channel's stored fallback, or ``None``) marks
    the pre-selected default; ``None`` selects the sentinel. Kept within
    Discord's 25-option limit by the catalog being <= 24 entries.
    """
    options: list[hikari.impl.SelectOptionBuilder] = [
        hikari.impl.SelectOptionBuilder(
            label="No fallback",
            value=SENTINEL_NO_FALLBACK,
            description="Don't retry with another model",
            is_default=current_fallback_key is None,
        )
    ]
    for family, models in models_by_family().items():
        for model in models:
            if model.key == primary_key:
                continue
            options.append(
                hikari.impl.SelectOptionBuilder(
                    label=f"{family} · {model.label}",
                    value=model.key,
                    is_default=model.key == current_fallback_key,
                )
            )
    return options


def create_settings_panel(
    model: CatalogModel,
    reasoning_level: str | None,
    auto_respond: bool,
    fallback_model_key: str | None,
) -> list[hikari.impl.MessageActionRowBuilder]:
    """Build the ephemeral settings panel for a chosen ``model``.

    Holds (in order) an optional reasoning select (reasoning-capable models
    only), a fallback-model select, and a button row with an auto-respond toggle
    and a "Budgets & filter…" button. The current panel state is encoded into
    every component's ``custom_id`` so each interaction can re-render the panel
    without server-side session state.
    """
    state = encode_panel_state(
        model.key, reasoning_level, auto_respond, fallback_model_key
    )
    rows: list[hikari.impl.MessageActionRowBuilder] = []

    if model.supports_reasoning:
        reasoning_row = hikari.impl.MessageActionRowBuilder()
        reasoning_menu = reasoning_row.add_text_menu(
            f"{REASONING_SELECT_CUSTOM_ID_PREFIX}:{state}",
            placeholder="Choose a reasoning level",
            min_values=1,
            max_values=1,
        )
        for option in build_reasoning_options(model, reasoning_level):
            reasoning_menu.add_option(
                option.label,
                option.value,
                description=option.description,
                is_default=option.is_default,
            )
        rows.append(reasoning_row)

    fallback_row = hikari.impl.MessageActionRowBuilder()
    fallback_menu = fallback_row.add_text_menu(
        f"{FALLBACK_SELECT_CUSTOM_ID_PREFIX}:{state}",
        placeholder="Choose a fallback model",
        min_values=1,
        max_values=1,
    )
    for option in build_fallback_options(model.key, fallback_model_key):
        fallback_menu.add_option(
            option.label,
            option.value,
            description=option.description,
            is_default=option.is_default,
        )
    rows.append(fallback_row)

    button_row = hikari.impl.MessageActionRowBuilder()
    button_row.add_interactive_button(
        hikari.ButtonStyle.SUCCESS if auto_respond else hikari.ButtonStyle.SECONDARY,
        f"{AUTO_TOGGLE_CUSTOM_ID_PREFIX}:{state}",
        label="Auto-respond: ON" if auto_respond else "Auto-respond: OFF",
    )
    button_row.add_interactive_button(
        hikari.ButtonStyle.PRIMARY,
        f"{CONTINUE_CUSTOM_ID_PREFIX}:{state}",
        label="Budgets & filter…",
    )
    rows.append(button_row)

    return rows


def create_settings_modal(
    state: PanelState,
    current: ChannelModelOverride | None,
) -> hikari.api.InteractionModalBuilder:
    """Build the budgets + response-filter modal carrying the full panel state.

    The ``custom_id`` encodes ``state`` so the submit handler persists model,
    reasoning, auto-respond and fallback alongside the free-text inputs. Budget
    and filter inputs prefill from ``current`` when present so reopening reflects
    the stored values.
    """
    modal = hikari.impl.InteractionModalBuilder(
        title="Channel Budgets & Filter",
        custom_id=(
            f"{MODAL_CUSTOM_ID_PREFIX}:"
            f"{encode_panel_state(state.model_key, state.reasoning_level, state.auto_respond, state.fallback_model_key)}"
        ),
    )
    # Discord rejects a text-input value outside 1-4000 chars, so an empty
    # prefill must be left UNDEFINED (omitted) rather than sent as "".
    daily_prefill = (
        str(current.daily_token_budget)
        if current is not None
        else hikari.UNDEFINED
    )
    hourly_prefill = (
        str(current.hourly_token_budget)
        if current is not None
        else hikari.UNDEFINED
    )
    filter_prefill = (
        current.response_filter
        if current is not None and current.response_filter
        else hikari.UNDEFINED
    )
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="daily_budget",
                label="Daily token budget (0 = unlimited)",
                style=hikari.TextInputStyle.SHORT,
                placeholder="0 = unlimited",
                value=daily_prefill,
                required=False,
                max_length=10,
            )
        )
    )
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="hourly_budget",
                label="Hourly token budget (0 = unlimited)",
                style=hikari.TextInputStyle.SHORT,
                placeholder="0 = unlimited",
                value=hourly_prefill,
                required=False,
                max_length=10,
            )
        )
    )
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="response_filter",
                label="Response filter (optional)",
                style=hikari.TextInputStyle.PARAGRAPH,
                placeholder=(
                    "When set, the bot only answers messages matching these "
                    'instructions — e.g. "Only respond to questions about '
                    'programming".'
                ),
                value=filter_prefill,
                required=False,
                max_length=4000,
            )
        )
    )
    return modal


def parse_budget(raw: str | None) -> int:
    """Parse a token-budget text input into a non-negative integer.

    Empty/whitespace/``None`` means "unset" -> ``0`` (unlimited). Anything that
    is not a non-negative base-10 integer, or that exceeds ``MAX_TOKEN_BUDGET``,
    raises ``ValueError`` (surfaced by the handler as an ephemeral error).
    """
    if raw is None:
        return 0
    text = raw.strip()
    if not text:
        return 0
    if not text.isdigit():
        raise ValueError(
            f"'{raw}' is not a valid budget — enter a whole number of tokens "
            f"(0 = unlimited)."
        )
    value = int(text)
    if value > MAX_TOKEN_BUDGET:
        raise ValueError(
            f"'{raw}' is too large — the maximum budget is "
            f"{MAX_TOKEN_BUDGET:,} tokens (0 = unlimited)."
        )
    return value
