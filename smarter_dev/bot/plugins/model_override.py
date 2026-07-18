"""Admin ``/chat-bot-settings`` slash command — per-channel LLM configuration.

Admin-gated (ADMINISTRATOR). Opens a model string-select for the current
channel; picking a model swaps the ephemeral message for one settings panel
(reasoning level for reasoning-capable models, a fallback model, and an
auto-respond toggle), and a "Budgets & filter…" button opens a modal for the
daily/hourly token budgets and an optional response filter. A separate Save
button persists panel changes immediately while retaining the stored budgets
and filter; modal submit persists everything in one call via
:class:`~smarter_dev.bot.services.model_override_service.ModelOverrideService`.
Picking the "server default" sentinel clears the override instead.

The interaction handlers here are dispatched from
:mod:`smarter_dev.bot.plugins.events` by ``custom_id``: the model select
(``handle_model_override_select``), the panel re-render handlers
(``handle_model_override_reasoning_select`` / ``_fallback_select`` /
``_auto_toggle``), the quick-save handler (``handle_model_override_save``), the
modal opener (``handle_model_override_continue``), and the modal submit
(``handle_model_override_modal_submit``). The chat runtime consumes the stored settings via
:func:`~smarter_dev.bot.agents.chat_agent.get_chat_agent`.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

import hikari
import lightbulb

from smarter_dev.bot.plugins.admin_gate import deny_if_not_admin
from smarter_dev.bot.plugins.admin_gate import is_admin
from smarter_dev.bot.services.channel_token_budget import fallback_ended_key
from smarter_dev.bot.services.channel_token_budget import fallback_flag_key
from smarter_dev.bot.services.chat_engine_registry import get_chat_engine_registry
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.model_override_service import ModelOverrideService
from smarter_dev.bot.services.models import ChannelModelOverride
from smarter_dev.bot.views.model_override_views import AUTO_TOGGLE_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import CONTINUE_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import FALLBACK_SELECT_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import MODAL_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import MODEL_NEXT_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import REASONING_SELECT_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import SAVE_CUSTOM_ID_PREFIX
from smarter_dev.bot.views.model_override_views import SENTINEL_DEFAULT
from smarter_dev.bot.views.model_override_views import SENTINEL_MODEL_DEFAULT
from smarter_dev.bot.views.model_override_views import SENTINEL_NO_FALLBACK
from smarter_dev.bot.views.model_override_views import PanelState
from smarter_dev.bot.views.model_override_views import create_model_select_message
from smarter_dev.bot.views.model_override_views import create_settings_modal
from smarter_dev.bot.views.model_override_views import create_settings_panel
from smarter_dev.bot.views.model_override_views import parse_budget
from smarter_dev.bot.views.model_override_views import parse_panel_state
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import is_valid_model_key
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.shared.model_catalog import resolve_reasoning_level

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("model_override")


ADMIN_DENIAL_MESSAGE = (
    "You need the Administrator permission to change chat-bot settings."
)


async def _deny_interaction_if_not_admin(
    event: hikari.InteractionCreateEvent,
) -> bool:
    """Re-check admin on a select/button/modal interaction (defense in depth — a
    raw custom_id could be replayed by a non-admin). Respond ephemerally on
    denial."""
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


def _render_fallback(fallback_model_key: str | None) -> str:
    """Human-render the fallback model for the confirmation message."""
    if fallback_model_key is None:
        return "none"
    fallback = get_model(fallback_model_key)
    return fallback.label if fallback is not None else fallback_model_key


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


async def _load_current_override(
    app: Any, guild_id: str, channel_id: str
) -> ChannelModelOverride | None:
    """Load the channel's current override for prefilling, degrading to ``None``
    on a read failure so a transient API error never blocks configuring."""
    try:
        service = _get_override_service(app)
        return await service.get_override(guild_id, channel_id)
    except APIError as exc:
        logger.warning("Could not load current model override for prefill: %s", exc)
        return None


async def _render_panel(
    interaction: hikari.ComponentInteraction, state: PanelState
) -> None:
    """Re-render the settings panel for ``state`` as a message update."""
    model = get_model(state.model_key)
    if model is None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ That model is no longer available. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return
    await interaction.create_initial_response(
        hikari.ResponseType.MESSAGE_UPDATE,
        content=(
            f"Configuring **{model.label}** for this channel. Adjust the settings "
            "below, then choose **Save** or open **Budgets & filter…**."
        ),
        components=create_settings_panel(
            model,
            state.reasoning_level,
            state.auto_respond,
            state.fallback_model_key,
        ),
    )


@plugin.command
@lightbulb.command(
    "chat-bot-settings",
    "Configure the LLM model, budgets and behaviour for this channel (admin only)",
)
@lightbulb.implements(lightbulb.SlashCommand)
async def chat_bot_settings(ctx: lightbulb.Context) -> None:
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
    """Handle the model string-select: clear on the sentinel, otherwise swap the
    message for the settings panel prefilled from the stored override."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    selected = interaction.values[0] if interaction.values else None
    await _advance_from_model_selection(event, interaction, selected)


async def handle_model_override_next(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Advance with the model already selected when the command was opened."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    prefix, separator, selected = interaction.custom_id.partition(":")
    if not separator or prefix != MODEL_NEXT_CUSTOM_ID_PREFIX:
        selected = None
    await _advance_from_model_selection(event, interaction, selected)


async def _advance_from_model_selection(
    event: hikari.InteractionCreateEvent,
    interaction: hikari.ComponentInteraction,
    selected: str | None,
) -> None:
    """Clear the override or render the panel for a selected model."""
    guild_id = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)

    if selected == SENTINEL_DEFAULT:
        service = _get_override_service(event.app)
        try:
            await service.clear_override(guild_id, channel_id)
        except APIError as exc:
            logger.error(
                "Failed to clear model override for channel %s: %s", channel_id, exc
            )
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                content="❌ Couldn't remove the override — please try again shortly.",
                components=[],
            )
            return
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="✅ Removed this channel's model override — using the server default.",
            components=[],
        )
        return

    if not selected or not is_valid_model_key(selected):
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ That model is no longer available. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return

    current = await _load_current_override(event.app, guild_id, channel_id)
    # A stored fallback equal to the newly chosen primary is meaningless (a model
    # never falls back to itself), so drop it — otherwise it would be excluded
    # from the select yet still ride in the panel state.
    fallback_key = current.fallback_model_key if current is not None else None
    if fallback_key == selected:
        fallback_key = None
    state = PanelState(
        model_key=selected,
        reasoning_level=current.reasoning_level if current is not None else None,
        auto_respond=current.auto_respond if current is not None else False,
        fallback_model_key=fallback_key,
    )
    await _render_panel(interaction, state)


async def handle_model_override_reasoning_select(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the reasoning string-select: re-render the panel with the chosen
    reasoning level (or unset for the model default)."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    state = parse_panel_state(interaction.custom_id, REASONING_SELECT_CUSTOM_ID_PREFIX)
    if state is None:
        logger.error("Invalid reasoning select custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Invalid request. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return

    chosen = interaction.values[0] if interaction.values else SENTINEL_MODEL_DEFAULT
    reasoning_level = None if chosen == SENTINEL_MODEL_DEFAULT else chosen
    await _render_panel(
        interaction,
        PanelState(
            model_key=state.model_key,
            reasoning_level=reasoning_level,
            auto_respond=state.auto_respond,
            fallback_model_key=state.fallback_model_key,
        ),
    )


async def handle_model_override_fallback_select(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the fallback string-select: re-render the panel with the chosen
    fallback model (or unset for no fallback)."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    state = parse_panel_state(interaction.custom_id, FALLBACK_SELECT_CUSTOM_ID_PREFIX)
    if state is None:
        logger.error("Invalid fallback select custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Invalid request. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return

    chosen = interaction.values[0] if interaction.values else SENTINEL_NO_FALLBACK
    fallback_model_key = None if chosen == SENTINEL_NO_FALLBACK else chosen
    await _render_panel(
        interaction,
        PanelState(
            model_key=state.model_key,
            reasoning_level=state.reasoning_level,
            auto_respond=state.auto_respond,
            fallback_model_key=fallback_model_key,
        ),
    )


async def handle_model_override_auto_toggle(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the auto-respond toggle button: flip the flag and re-render."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    state = parse_panel_state(interaction.custom_id, AUTO_TOGGLE_CUSTOM_ID_PREFIX)
    if state is None:
        logger.error("Invalid auto-toggle custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Invalid request. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return

    await _render_panel(
        interaction,
        PanelState(
            model_key=state.model_key,
            reasoning_level=state.reasoning_level,
            auto_respond=not state.auto_respond,
            fallback_model_key=state.fallback_model_key,
        ),
    )


async def handle_model_override_continue(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the "Budgets & filter…" button: open the settings modal carrying
    the full panel state, budget/filter inputs prefilled from the stored value."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    state = parse_panel_state(interaction.custom_id, CONTINUE_CUSTOM_ID_PREFIX)
    if state is None or get_model(state.model_key) is None:
        logger.error("Invalid continue custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Invalid request. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return

    guild_id = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    current = await _load_current_override(event.app, guild_id, channel_id)

    modal = create_settings_modal(state, current)
    await interaction.create_modal_response(
        modal.title,
        modal.custom_id,
        components=modal.components,
    )


async def handle_model_override_save(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Save panel settings without opening the budgets/filter modal.

    Existing budgets and response filter are retained; a new override starts
    with unlimited budgets and no filter.
    """
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    state = parse_panel_state(interaction.custom_id, SAVE_CUSTOM_ID_PREFIX)
    model = get_model(state.model_key) if state is not None else None
    if state is None or model is None:
        logger.error("Invalid save custom_id: %s", interaction.custom_id)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Invalid request. Please run `/chat-bot-settings` again.",
            components=[],
        )
        return

    service = _get_override_service(event.app)
    guild_id = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)
    try:
        current = await service.get_override(guild_id, channel_id)
        daily_budget = current.daily_token_budget if current is not None else 0
        hourly_budget = current.hourly_token_budget if current is not None else 0
        response_filter = current.response_filter if current is not None else None
        await service.set_override(
            guild_id,
            channel_id,
            state.model_key,
            daily_budget,
            hourly_budget,
            reasoning_level=state.reasoning_level,
            auto_respond=state.auto_respond,
            fallback_model_key=state.fallback_model_key,
            response_filter=response_filter,
        )
    except APIError as exc:
        logger.error(
            "Failed to save model override for channel %s: %s", channel_id, exc
        )
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            content="❌ Couldn't save the override — please try again shortly.",
            components=[],
        )
        return

    await interaction.create_initial_response(
        hikari.ResponseType.MESSAGE_UPDATE,
        content=(
            f"✅ This channel now uses **{model.label}**.\n"
            f"• Reasoning: {_render_reasoning(model, state.reasoning_level)}\n"
            f"• Auto-respond: {'on' if state.auto_respond else 'off'}\n"
            f"• Fallback model: {_render_fallback(state.fallback_model_key)}\n"
            f"• Response filter: {'set' if response_filter else 'none'}\n"
            f"• Daily budget: {_render_budget(daily_budget)}\n"
            f"• Hourly budget: {_render_budget(hourly_budget)}"
        ),
        components=[],
    )


async def handle_model_override_modal_submit(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the settings modal submit: validate, persist everything, confirm."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ModalInteraction):
        return
    if await _deny_interaction_if_not_admin(event):
        return

    state = parse_panel_state(interaction.custom_id, MODAL_CUSTOM_ID_PREFIX)
    if state is None:
        logger.error(
            "Invalid model override modal custom_id: %s", interaction.custom_id
        )
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="❌ Invalid request. Please run `/chat-bot-settings` again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    model = get_model(state.model_key)
    if model is None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="❌ That model is no longer available. Please run `/chat-bot-settings` again.",
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

    filter_text = (values.get("response_filter") or "").strip()
    response_filter = filter_text or None

    service = _get_override_service(event.app)
    try:
        await service.set_override(
            str(interaction.guild_id),
            str(interaction.channel_id),
            state.model_key,
            daily_budget,
            hourly_budget,
            reasoning_level=state.reasoning_level,
            auto_respond=state.auto_respond,
            fallback_model_key=state.fallback_model_key,
            response_filter=response_filter,
        )
    except APIError as exc:
        # Without this, the failure surfaces as Discord's generic
        # "This interaction failed" (the events.py fallback responder is
        # broken) and the admin gets no explanation.
        logger.error(
            "Failed to save model override for channel %s: %s",
            interaction.channel_id,
            exc,
        )
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="❌ Couldn't save the override — please try again shortly.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    await interaction.create_initial_response(
        hikari.ResponseType.MESSAGE_CREATE,
        content=(
            f"✅ This channel now uses **{model.label}**.\n"
            f"• Reasoning: {_render_reasoning(model, state.reasoning_level)}\n"
            f"• Auto-respond: {'on' if state.auto_respond else 'off'}\n"
            f"• Fallback model: {_render_fallback(state.fallback_model_key)}\n"
            f"• Response filter: {'set' if response_filter else 'none'}\n"
            f"• Daily budget: {_render_budget(daily_budget)}\n"
            f"• Hourly budget: {_render_budget(hourly_budget)}"
        ),
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def _budget_redis(app: Any) -> Any | None:
    """The bot's shared chat-memory Redis (used for the per-channel budgets), or
    ``None`` when unavailable/tests use a non-dict ``bot.d``."""
    data = getattr(app, "d", None)
    if not isinstance(data, dict):
        return None
    return data.get("chat_memory_redis")


# Seconds the "primary is back" marker outlives the budget reset — one day, so a
# channel still learns its primary returned even if it goes quiet after opting in.
_FALLBACK_ENDED_MARKER_TTL_SECONDS = 86400


async def handle_model_budget_fallback(
    event: hikari.InteractionCreateEvent,
) -> None:
    """Handle the "Answer with <fallback> instead" button on a budget-exhausted
    notice.

    Member-facing with NO admin gate — any member may opt the channel into the
    free fallback model while the primary's budget is spent. On press: re-read
    the override; if no fallback is configured any more, say so; otherwise mark
    the channel's fallback active in Redis (both keys expire around the budget
    reset the button was minted with), update the notice to confirm and drop the
    button, and fire the channel's engine so pending conversation resumes.
    """
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return

    parts = interaction.custom_id.split(":")
    if len(parts) != 2:
        logger.error(
            "Invalid model-budget-fallback custom_id: %s", interaction.custom_id
        )
        return
    try:
        reset_epoch = int(parts[1])
    except ValueError:
        logger.error(
            "Non-numeric reset epoch in model-budget-fallback custom_id: %s",
            interaction.custom_id,
        )
        return

    guild_id = str(interaction.guild_id)
    channel_id = str(interaction.channel_id)

    override = await _load_current_override(event.app, guild_id, channel_id)
    if override is None or override.fallback_model_key is None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="This channel no longer has a fallback model configured.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    now_epoch = int(datetime.now(UTC).timestamp())
    if reset_epoch <= now_epoch:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="The budget has already reset — the primary model is answering again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    redis = _budget_redis(event.app)
    if redis is None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="Sorry, couldn't switch to the fallback model right now — please try again shortly.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    try:
        await redis.set(fallback_flag_key(channel_id), "1", exat=reset_epoch)
        await redis.set(
            fallback_ended_key(channel_id),
            "1",
            exat=reset_epoch + _FALLBACK_ENDED_MARKER_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Failed to set fallback keys for channel %s: %s", channel_id, exc
        )
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            content="Sorry, couldn't switch to the fallback model right now — please try again shortly.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    fallback_model = get_model(override.fallback_model_key)
    label = (
        fallback_model.label
        if fallback_model is not None
        else override.fallback_model_key
    )
    await interaction.create_initial_response(
        hikari.ResponseType.MESSAGE_UPDATE,
        content=(
            f"Switched to **{label}** while the primary model's budget is spent — "
            "it'll answer pending messages now."
        ),
        components=[],
    )

    # Resume any active engagement so queued messages get answered immediately
    # rather than waiting on the next message to fire the engine.
    try:
        engine = await get_chat_engine_registry().get(int(channel_id))
    except Exception:
        logger.debug(
            "could not resolve chat engine for channel %s", channel_id, exc_info=True
        )
        engine = None
    if engine is not None and engine.active:
        engine.fire_now()


def load(bot: lightbulb.BotApp) -> None:
    """Load the model-override plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the model-override plugin."""
    bot.remove_plugin(plugin)
