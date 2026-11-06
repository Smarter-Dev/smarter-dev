"""Interactive challenge / daily quest solution submission modal for Discord bot."""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

import hikari

if TYPE_CHECKING:
    from smarter_dev.bot.services.api_client import APIClient

from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.exceptions import RateLimitError

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# MODAL FACTORY
# ─────────────────────────────────────────────────────────────

def create_solution_submission_modal(
    challenge_id: str,
    challenge_title: str = "Challenge",
    modal_prefix: str = "submit_solution_modal",
) -> hikari.api.InteractionModalBuilder:
    modal = hikari.impl.InteractionModalBuilder(
        title=f"Submit Solution - {challenge_title[:30]}",
        custom_id=f"{modal_prefix}:{challenge_id}",
    )

    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="solution",
                label="Your Solution",
                placeholder="Enter your solution here...",
                required=True,
                min_length=1,
                max_length=4000,
                style=hikari.TextInputStyle.PARAGRAPH,
            )
        )
    )

    return modal


# ─────────────────────────────────────────────────────────────
# MODAL HANDLER
# ─────────────────────────────────────────────────────────────

class SolutionSubmissionModalHandler:
    def __init__(
        self,
        challenge_id: str,
        challenge_title: str,
        guild_id: str,
        user: hikari.User,
        api_client: APIClient,
        endpoint: str | None = None,
    ):
        self.challenge_id = challenge_id
        self.challenge_title = challenge_title
        self.guild_id = guild_id
        self.user = user
        self.api_client = api_client
        self.endpoint = endpoint or f"/challenges/{challenge_id}/submit-solution"

    async def handle_submit(self, interaction: hikari.ModalInteraction) -> None:
        solution: str | None = None

        for row in interaction.components:
            for component in getattr(row, "components", []):
                if component.custom_id == "solution":
                    solution = component.value
                    break

        if not solution or not solution.strip():
            try:
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Solution cannot be empty.",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
            except hikari.errors.AlreadyRespondedError:
                pass
            return

        try:
            response = await self.api_client.post(
                self.endpoint,
                json_data={
                    "guild_id": self.guild_id,
                    "user_id": str(self.user.id),
                    "username": self.user.display_name or self.user.username,
                    "submitted_solution": solution.strip(),
                },
            )

            if response.status_code != 200:
                try:
                    await interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_CREATE,
                        content="❌ Failed to submit solution.",
                        flags=hikari.MessageFlag.EPHEMERAL,
                    )
                except hikari.errors.AlreadyRespondedError:
                    pass
                return

            data = response.json()
            is_correct = data.get("is_correct", False)
            is_first = data.get("is_first_success", False)
            points = data.get("points_earned")

            user_mention = f"<@{self.user.id}>"

            is_daily_quest = self.endpoint.startswith("/quests/")

            # Default: private feedback
            flags = hikari.MessageFlag.EPHEMERAL

            if not is_correct:
                msg = "❌ **Incorrect Solution**\n\nTry again."

            else:
                # ---- DAILY QUESTS ----
                if is_daily_quest:
                    flags = hikari.MessageFlag.NONE  # public

                    if is_first:
                        msg = (
                            f"{user_mention} has submitted the **first correct solution** "
                            f"to the **Daily Quest** and earned their squad **{points} points!** 🏆"
                        )
                    else:
                        msg = (
                            f"{user_mention} has submitted a **correct solution** "
                            f"to the **Daily Quest** ✅"
                        )

                # ---- CHALLENGES (unchanged behaviour) ----
                else:
                    msg = (
                        f"{user_mention}\n"
                        f"✅ **Correct Solution!**\n\n"
                        f"🏆 First success: **{is_first}**\n"
                        f"🎯 Points earned: **{points}**"
                    )
                    flags = hikari.MessageFlag.NONE

            try:
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content=msg,
                    flags=flags,
                )
            except hikari.errors.AlreadyRespondedError:
                pass

        except RateLimitError as e:
            cooldown_message = _cooldown_message(e)
            if cooldown_message is None:
                logger.error(f"API error submitting solution: {e}")
            try:
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content=cooldown_message or "❌ Submission failed.",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
            except hikari.errors.AlreadyRespondedError:
                pass

        except APIError as e:
            logger.error(f"API error submitting solution: {e}")
            try:
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Submission failed.",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
            except hikari.errors.AlreadyRespondedError:
                pass

        except Exception as e:
            logger.exception(f"Unhandled submission error: {e}")
            try:
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Unexpected error.",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
            except hikari.errors.AlreadyRespondedError:
                pass


def _cooldown_message(error: RateLimitError) -> str | None:
    """The squad-cooldown reply for a 429 from the challenge submit route, else None."""
    body = error.context.get("response")
    retry_after = body.get("retry_after") if isinstance(body, dict) else None
    if not isinstance(retry_after, int):
        return None
    retry_at = datetime.now(UTC) + timedelta(seconds=retry_after)
    return (
        "⏳ **Your squad is on cooldown** after a wrong answer.\n\n"
        f"You can submit again <t:{int(retry_at.timestamp())}:R>."
    )
