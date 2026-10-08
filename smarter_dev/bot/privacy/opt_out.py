"""The AI assistant opt-out control behind ``/privacy`` (#92).

``/privacy`` carries one button that opens the control: an ephemeral message
saying whether the person is opted out, what that means, and one button to
change it. Discord modals cannot hold a toggle, so the control is a message.

Every custom id ends with the id of the person it was shown to, and every
handler checks it against the clicking user before doing anything. The id
sent to the bot API is always the clicking user's, from the interaction,
never one read from the custom id. Ephemeral messages already reach only
their recipient; the check is what keeps a copied custom id useless.

The web app holds the state (``smarter_dev.web.chat_bot_opt_out``); a change
reaches both runtimes with their next block-list refresh, within 60 seconds.
An opt-out also blocks the person in this process the moment the web app
confirms it and asks for a refresh at once (#100), so the bot that showed the
button never answers them in that window.
"""

from __future__ import annotations

import logging
from typing import Any

import hikari

from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.services.privacy_service import OptOut

logger = logging.getLogger(__name__)

CUSTOM_ID_PREFIX = "ai_opt_out:"
OPEN = "open"
SET = "set"
CLEAR = "clear"

NOT_OPTED_OUT = "You are not opted out of the AI assistant."
OPTED_OUT = "You are opted out of the AI assistant."
OPTED_OUT_BY_DELETION = (
    "You are opted out of the AI assistant because your data was deleted at "
    "your request. That opt-out stays, so it cannot be changed here."
)
EXPLANATION = (
    "Opting out stops the AI assistant reading or answering your messages from "
    "now on. It does not delete what it already holds; to have that deleted, "
    "ask an @admin or email admin@smarter.dev. Opting back in applies to new "
    "messages only."
)
NOT_YOURS = "This control belongs to someone else. Run /privacy to get your own."
UNAVAILABLE = "Your opt-out could not be read or changed just now. Try again in a minute."
OPEN_LABEL = "AI assistant opt-out"


def custom_id(action: str, user_id: Any) -> str:
    return f"{CUSTOM_ID_PREFIX}{action}:{user_id}"


def parse_custom_id(value: str) -> tuple[str, str] | None:
    """``(action, user id)`` for one of this control's ids, else None."""
    if not value.startswith(CUSTOM_ID_PREFIX):
        return None
    action, _, user_id = value.removeprefix(CUSTOM_ID_PREFIX).partition(":")
    if action not in (OPEN, SET, CLEAR) or not user_id:
        return None
    return action, user_id


def open_button(user_id: Any) -> hikari.impl.MessageActionRowBuilder:
    """The row ``/privacy`` adds under its summary."""
    row = hikari.impl.MessageActionRowBuilder()
    row.add_interactive_button(
        hikari.ButtonStyle.SECONDARY, custom_id(OPEN, user_id), label=OPEN_LABEL
    )
    return row


def render(state: OptOut, user_id: Any) -> tuple[str, list]:
    """The control's text and components for ``state``."""
    if state.opted_out and state.source == "purge":
        return f"**{OPTED_OUT_BY_DELETION}**\n\n{EXPLANATION}", []
    row = hikari.impl.MessageActionRowBuilder()
    if state.opted_out:
        row.add_interactive_button(
            hikari.ButtonStyle.SECONDARY, custom_id(CLEAR, user_id), label="Opt back in"
        )
        return f"**{OPTED_OUT}**\n\n{EXPLANATION}", [row]
    row.add_interactive_button(
        hikari.ButtonStyle.DANGER, custom_id(SET, user_id), label="Opt out"
    )
    return f"**{NOT_OPTED_OUT}**\n\n{EXPLANATION}", [row]


async def handle_interaction(event: hikari.InteractionCreateEvent, privacy_service) -> bool:
    """Handle a click on the control. False when it is not this control's."""
    interaction = event.interaction
    if not isinstance(interaction, hikari.ComponentInteraction):
        return False
    parsed = parse_custom_id(interaction.custom_id)
    if parsed is None:
        return False
    action, shown_to = parsed
    user_id = str(interaction.user.id)
    if shown_to != user_id:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            NOT_YOURS,
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True

    # Open answers with a new ephemeral message; set and clear edit the control.
    if action == OPEN:
        await interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL,
        )
    else:
        await interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_UPDATE
        )
    try:
        if privacy_service is None:
            raise RuntimeError("no privacy service")
        if action == OPEN:
            state = await privacy_service.get_opt_out(user_id)
        else:
            state = await privacy_service.set_opt_out(user_id, opted_out=action == SET)
    except Exception as error:  # noqa: BLE001 — the person gets a sentence, not a trace
        # The type only: never who asked.
        logger.warning("AI assistant opt-out %s failed (%s)", action, type(error).__name__)
        await interaction.edit_initial_response(UNAVAILABLE, components=[])
        return True
    if action == SET and state.opted_out:
        get_blocked_users().block_now(user_id, state.revision)
    content, components = render(state, user_id)
    await interaction.edit_initial_response(content, components=components)
    return True
