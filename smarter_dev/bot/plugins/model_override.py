"""Admin ``/setmodel`` slash command — per-channel LLM model override.

Admin-gated (ADMINISTRATOR). Opens a model string-select for the current
channel; a reasoning-capable model then shows a reasoning-level select, after
which a token-budget modal persists the override (model + reasoning + budgets)
via :class:`~smarter_dev.bot.services.model_override_service.ModelOverrideService`.
Models with no reasoning knob skip straight to the budgets modal. Picking the
"server default" sentinel clears the override instead.

The three interaction handlers here (``handle_model_override_select`` /
``handle_model_override_reasoning_select`` / ``handle_model_override_modal_submit``)
are dispatched from :mod:`smarter_dev.bot.plugins.events` by ``custom_id``. The
chat runtime consumes the stored model + reasoning level via
:func:`~smarter_dev.bot.agents.chat_agent.get_chat_agent`.
"""

from __future__ import annotations

import logging
from typing import Any

import hikari
import lightbulb

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import is_valid_model_key
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.shared.model_catalog import resolve_reasoning_level
from smarter_dev.bot.plugins.admin_gate import deny_if_not_admin
from smarter_dev.bot.plugins.admin_gate import is_admin
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.model_override_service import ModelOverrideService
from smarter_dev.bot.services.models import ChannelModelOverride
from smarter_dev.bot.views.model_override_views import MODAL_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import (
    REASONING_SELECT_CUSTOM_ID_PREFIX,
)
from smarter_dev.bot.views.model_override_views import SENTINEL_DEFAULT
from smarter_dev.bot.views.model_override_views import SENTINEL_MODEL_DEFAULT
from smarter_dev.bot.views.model_override_views import create_budgets_modal
from smarter_dev.bot.views.model_override_views import (
    create_model_select_message,
)
from smarter_dev.bot.views.model_override_views import (
    create_reasoning_select_message,
)
from smarter_dev.bot.views.model_override_views import parse_budget

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("model_override")


ADMIN_DENIAL_MESSAGE = (
    "You need the Administrator permission to set a channel model."
)


async def _deny_interaction_if_not_admin(
    event: hikari.InteractionCreateEvent,
) -> bool:
    """Re-check admin on a select/modal interaction (defense in depth — a raw
    custom_id could be replayed by a non-admin). Respond ephemerally on denial."""
    member = event.interaction.member
    if not isinstance(member, hikari.InteractionMember) or not is_admin(
        lightbulb.utils.permissions_for(member)
    ):
        await event.interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content=ADMIN_DENIAL_MESSAGE,
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True
    return False


def _get_override_service(bot: Any) -> ModelOverrideService:
    """Resolve the model-override service from bot data (dict or DataStore)."""
    data = bot.d
    service = None
    if isinstance(data, dict):
        service = data.get("model_override_service")
        if service is None:
            service = data.get("_services", {}).get("model_override_service")
    else:
        service = getattr(data, "model_override_service", None)
        if service is None:
            services = getattr(data, "_services", None) or {}
            service = services.get("model_override_service")
    if service is None:
        raise RuntimeError("model_override_service is not registered on the bot")
    return service


def _render_budget(tokens: int) -> str:
    """Human-render a token budget; ``0`` means unlimited."""
    return "unlimited" if tokens == 0 else f"{tokens:,} tokens"


def _render_reasoning(model: CatalogModel, reasoning_level: str | None) -> str:
    """Human-render the effective reasoning level for the confirmation message.

    Shows the level actually applied (clamped to what ``model`` supports), noting
    when it falls back to the model default or the model has no reasoning knob.
    """
    if not model.supports_reasoning:
        return "not applicable for this model"
    requested = parse_reasoning_level(reasoning_level)
    effective = resolve_reasoning_level(model, requested)
    if requested is None:
        return f"{effective.label} (model default)"
    if effective is not requested:
        return f"{effective.label} (adjusted to fit {model.label})"
    return effective.label


def _read_modal_text_values(
    interaction: hikari.ModalInteraction,
) -> dict[str, str]:
    """Collect ``custom_id -> value`` for every text input in a modal submit."""
    values: dict[str, str] = {}
    for action_row in interaction.components:
        for component in action_row:
            component_id = getattr(component, "custom_id", None)
            if component_id is not None:
                values[component_id] = component.value
    return values


@plugin.command
@lightbulb.command(
    "setmodel",
    "Set the LLM model and token budgets for this channel (admin only)",
)
@lightbulb.implements(lightbulb.SlashCommand)
async def setmodel(ctx: lightbulb.Context) -> None:
    """Open the model-selection dropdown for the current channel."""
    if await deny_if_not_admin(ctx, ADMIN_DENIAL_MESSAGE):
        return

    guild_id = str(ctx.guild_id)
    channel_id = str(ctx.channel_id)

    current: ChannelModelOverride | None = None
    try:
        service = _get_override_service(ctx.bot)
        current = await service.get_override(guild_id, channel_id)
    except APIError as exc:
        # A read failure shouldn't block configuring — show the menu unprefilled.
        logger.warning("Could not load current model override for prefill: %s", exc)

    await ctx.respond(
        "Select a model for this channel. Pick **Server default** to remove any override.",
        components=create_model_select_message(current),
        flags=hikari.MessageFlag.EPHEMERAL,
    )


async def handle_model_override_select(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the model string-select: clear on sentinel; for a reasoning-capable
    model show the reasoning select next; otherwise open the budgets modal."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    guild_id = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    selected = interaction.values[0] if interaction.values else None

    if selected == SENTINEL_DEFAULT:
        service = _get_override_service(event.app)
        await service.clear_override(guild_id, channel_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="✅ Removed this channel's model override — using the server default.",
            components=[],
        )
        return

    if not selected or not is_valid_model_key(selected):
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ That model is no longer available. Please run `/setmodel` again.",
            components=[],
        )
        return

    model = get_model(selected)
    service = _get_override_service(event.app)
    current: ChannelModelOverride | None = None
    try:
        current = await service.get_override(guild_id, channel_id)
    except APIError as exc:
        logger.warning("Could not load override for prefill: %s", exc)

    if model.supports_reasoning:
        # Reasoning-capable models get a second select before the budgets modal.
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content=(
                f"Selected **{model.label}**. Now choose a reasoning level, "
                "then set token budgets."
            ),
            components=create_reasoning_select_message(
                guild_id, channel_id, model, current
            ),
        )
        return

    # No reasoning knob — go straight to the budgets modal (reasoning stays unset).
    modal = create_budgets_modal(guild_id, channel_id, selected, None, current)
    await interaction.create_modal_response(
        modal.title,
        modal.custom_id,
        components=modal.components,
    )


async def handle_model_override_reasoning_select(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the reasoning string-select: open the budgets modal carrying the
    chosen reasoning level (or unset for the model default)."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    parts = interaction.custom_id.split(":")
    if len(parts) != 4 or parts[0] != REASONING_SELECT_CUSTOM_ID_PREFIX:
        logger.error("Invalid reasoning select custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Invalid request. Please run `/setmodel` again.",
            components=[],
        )
        return

    _, guild_id, channel_id, model_key = parts
    if get_model(model_key) is None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ That model is no longer available. Please run `/setmodel` again.",
            components=[],
        )
        return

    chosen = interaction.values[0] if interaction.values else SENTINEL_MODEL_DEFAULT
    reasoning_level = None if chosen == SENTINEL_MODEL_DEFAULT else chosen

    service = _get_override_service(event.app)
    current: ChannelModelOverride | None = None
    try:
        current = await service.get_override(guild_id, channel_id)
    except APIError as exc:
        logger.warning("Could not load override for budget prefill: %s", exc)

    modal = create_budgets_modal(
        guild_id, channel_id, model_key, reasoning_level, current
    )
    await interaction.create_modal_response(
        modal.title,
        modal.custom_id,
        components=modal.components,
    )


async def handle_model_override_modal_submit(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the budgets modal submit: validate, persist, confirm ephemerally."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ModalInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    parts = interaction.custom_id.split(":")
    if len(parts) != 5 or parts[0] != MODAL_CUSTOM_ID_PREFIX:
        logger.error("Invalid model override modal custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="❌ Invalid request. Please run `/setmodel` again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    _, guild_id, channel_id, model_key, reasoning_part = parts
    reasoning_level = reasoning_part or None
    model = get_model(model_key)
    if model is None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="❌ That model is no longer available. Please run `/setmodel` again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    values = _read_modal_text_values(interaction)
    try:
        daily_budget = parse_budget(values.get("daily_budget"))
        hourly_budget = parse_budget(values.get("hourly_budget"))
    except ValueError as exc:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content=f"❌ {exc}",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    service = _get_override_service(event.app)
    await service.set_override(
        guild_id,
        channel_id,
        model_key,
        daily_budget,
        hourly_budget,
        reasoning_level=reasoning_level,
    )

    await interaction.create_initial_response(
        hikari.ResponseType.MESSAGE_CREATE,
        content=(
            f"✅ This channel now uses **{model.label}**.\n"
            f"• Reasoning: {_render_reasoning(model, reasoning_level)}\n"
            f"• Daily budget: {_render_budget(daily_budget)}\n"
            f"• Hourly budget: {_render_budget(hourly_budget)}"
        ),
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def load(bot: lightbulb.BotApp) -> None:
    """Load the model-override plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the model-override plugin."""
    bot.remove_plugin(plugin)
