"""Discord components for the admin per-channel model-override command.

Path B (two-step) flow — chosen because the installed hikari (2.1.1) exposes no
``Label`` component builder, so Discord modal string-selects are unsupported:

1. ``/setmodel`` responds (ephemeral) with a message string-select of models
   (``create_model_select_message``), plus a "server default" sentinel option.
2. Picking a model opens a small budgets modal (``create_budgets_modal``) whose
   ``custom_id`` encodes the guild, channel, and chosen model key; picking the
   sentinel clears the override without a modal.

Everything here is a pure builder or parser — the actual API writes and Discord
responses live in :mod:`smarter_dev.bot.plugins.model_override`.
"""

from __future__ import annotations

import hikari

from smarter_dev.bot.agents.model_catalog import models_by_family
from smarter_dev.bot.services.models import ChannelModelOverride

# Select value that means "remove any override and fall back to the server
# default model". Kept distinct from every catalog key.
SENTINEL_DEFAULT = "__default__"

# custom_id prefixes routed in smarter_dev.bot.plugins.events.
SELECT_CUSTOM_ID = "model_override_select"
MODAL_CUSTOM_ID_PREFIX = "model_override_modal"


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


def create_budgets_modal(
    guild_id: str,
    channel_id: str,
    model_key: str,
    current: ChannelModelOverride | None,
) -> hikari.api.InteractionModalBuilder:
    """Build the daily/hourly token-budget modal for a chosen model.

    The ``custom_id`` carries ``guild_id``, ``channel_id`` and ``model_key`` so
    the submit handler can persist without extra state. Budget inputs prefill
    from ``current`` when present so reopening reflects the stored values.
    """
    modal = hikari.impl.InteractionModalBuilder(
        title="Channel Token Budgets",
        custom_id=f"{MODAL_CUSTOM_ID_PREFIX}:{guild_id}:{channel_id}:{model_key}",
    )
    daily_prefill = str(current.daily_token_budget) if current is not None else ""
    hourly_prefill = str(current.hourly_token_budget) if current is not None else ""
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="daily_budget",
                label="Daily token budget (0 = unlimited)",
                style=hikari.TextInputStyle.SHORT,
                placeholder="0 = unlimited",
                value=daily_prefill,
                required=False,
                max_length=15,
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
                max_length=15,
            )
        )
    )
    return modal


def parse_budget(raw: str | None) -> int:
    """Parse a token-budget text input into a non-negative integer.

    Empty/whitespace/``None`` means "unset" -> ``0`` (unlimited). Anything that
    is not a non-negative base-10 integer raises ``ValueError`` (surfaced by the
    handler as an ephemeral error).
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
    return int(text)
