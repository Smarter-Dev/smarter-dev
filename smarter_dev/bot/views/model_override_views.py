"""Discord components for the admin per-channel model-override command.

Path B (multi-step) flow — chosen because the installed hikari (2.1.1) exposes no
``Label`` component builder, so Discord modal string-selects are unsupported. The
reasoning level therefore lives in its own *message* select (Discord modals
cannot host selects), not in the budgets modal:

1. ``/setmodel`` responds (ephemeral) with a message string-select of models
   (``create_model_select_message``), plus a "server default" sentinel option.
2. Picking a reasoning-capable model swaps the message for a reasoning-level
   select (``create_reasoning_select_message``); picking a model with no
   reasoning knob skips straight to step 3.
3. The budgets modal (``create_budgets_modal``) whose ``custom_id`` encodes the
   guild, channel, chosen model key and reasoning level. Picking the server
   default sentinel clears the override without a modal.

Everything here is a pure builder or parser — the actual API writes and Discord
responses live in :mod:`smarter_dev.bot.plugins.model_override`.
"""

from __future__ import annotations

import hikari

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import models_by_family
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.bot.services.models import ChannelModelOverride

# Select value that means "remove any override and fall back to the server
# default model". Kept distinct from every catalog key.
SENTINEL_DEFAULT = "__default__"

# Reasoning-select value that means "use the model's own default level" (stored
# as NULL). Kept distinct from every ReasoningLevel value.
SENTINEL_MODEL_DEFAULT = "__model_default__"

# custom_id prefixes routed in smarter_dev.bot.plugins.events.
SELECT_CUSTOM_ID = "model_override_select"
REASONING_SELECT_CUSTOM_ID_PREFIX = "model_override_reasoning"
MODAL_CUSTOM_ID_PREFIX = "model_override_modal"

# Server-side cap on a token budget (matches the API/DB 32-bit signed maximum).
# Enforced here too so an over-limit entry gets a clear message instead of
# Discord's generic "This interaction failed".
MAX_TOKEN_BUDGET = 2_147_483_647


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


def create_reasoning_select_message(
    guild_id: str,
    channel_id: str,
    model: CatalogModel,
    current: ChannelModelOverride | None,
) -> list[hikari.impl.MessageActionRowBuilder]:
    """Build the ephemeral reasoning-select step for a chosen reasoning-capable
    model. The ``custom_id`` carries ``guild_id``, ``channel_id`` and the model
    key so the next handler can open the budgets modal without extra state."""
    current_level = current.reasoning_level if current is not None else None
    action_row = hikari.impl.MessageActionRowBuilder()
    menu = action_row.add_text_menu(
        f"{REASONING_SELECT_CUSTOM_ID_PREFIX}:{guild_id}:{channel_id}:{model.key}",
        placeholder="Choose a reasoning level",
        min_values=1,
        max_values=1,
    )
    for option in build_reasoning_options(model, current_level):
        menu.add_option(
            option.label,
            option.value,
            description=option.description,
            is_default=option.is_default,
        )
    return [action_row]


def create_budgets_modal(
    guild_id: str,
    channel_id: str,
    model_key: str,
    reasoning_level: str | None,
    current: ChannelModelOverride | None,
) -> hikari.api.InteractionModalBuilder:
    """Build the daily/hourly token-budget modal for a chosen model.

    The ``custom_id`` carries ``guild_id``, ``channel_id``, ``model_key`` and the
    chosen ``reasoning_level`` (empty for "model default") so the submit handler
    can persist without extra state. Budget inputs prefill from ``current`` when
    present so reopening reflects the stored values.
    """
    modal = hikari.impl.InteractionModalBuilder(
        title="Channel Token Budgets",
        custom_id=(
            f"{MODAL_CUSTOM_ID_PREFIX}:{guild_id}:{channel_id}:{model_key}"
            f":{reasoning_level or ''}"
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
