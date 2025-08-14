"""Interactive challenge solution submission modal for Discord bot.

This module provides modal components for challenge solution submission operations,
including solution text input with validation.
"""

from __future__ import annotations

import hikari
import logging
from typing import TYPE_CHECKING, Optional, Dict, Any

if TYPE_CHECKING:
    from smarter_dev.bot.services.api_client import APIClient

from smarter_dev.bot.services.exceptions import APIError

logger = logging.getLogger(__name__)


def create_solution_submission_modal(
    challenge_id: str,
    challenge_title: str = "Challenge"
) -> hikari.api.InteractionModalBuilder:
    """Create a solution submission modal.
    
    Args:
        challenge_id: UUID of the challenge
        challenge_title: Title of the challenge for display
        
    Returns:
        The modal builder instance
    """
    modal = hikari.impl.InteractionModalBuilder(
        title=f"Submit Solution - {challenge_title[:30]}",
        custom_id=f"submit_solution_modal:{challenge_id}"
    )
    
    # Add solution text input
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="solution",
                label="Your Solution",
                placeholder="Enter your solution here...",
                required=True,
                min_length=1,
                max_length=4000,
                style=hikari.TextInputStyle.PARAGRAPH
            )
        )
    )
    
    return modal


class SolutionSubmissionModalHandler:
    """Handler for solution submission modal interactions."""
    
    def __init__(
        self,
        challenge_id: str,
        challenge_title: str,
        guild_id: str,
        user: hikari.User,
        api_client: 'APIClient'
    ):
        """Initialize the modal handler.
        
        Args:
            challenge_id: UUID of the challenge
            challenge_title: Title of the challenge
            guild_id: Discord guild ID
            user: User submitting the solution
            api_client: API client for backend communication
        """
        self.challenge_id = challenge_id
        self.challenge_title = challenge_title
        self.guild_id = guild_id
        self.user = user
        self.api_client = api_client
    
    async def handle_submit(self, interaction: hikari.ModalInteraction) -> None:
        """Handle modal submission and process the solution submission.
        
        Args:
            interaction: The modal interaction
        """
        try:
            # Get the solution input value
            solution = None
            
            for component in interaction.components:
                if hasattr(component, 'components'):
                    for text_input in component.components:
                        if text_input.custom_id == "solution":
                            solution = text_input.value
                            break
            
            if not solution or not solution.strip():
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Solution cannot be empty. Please provide your solution.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return
            
            # Submit solution to API
            logger.info(f"Submitting solution for challenge {self.challenge_id} by user {self.user.id}")
            
            try:
                response = await self.api_client.post(
                    f"/challenges/{self.challenge_id}/submit-solution",
                    json_data={
                        "guild_id": self.guild_id,
                        "user_id": str(self.user.id),
                        "username": self.user.display_name or self.user.username,
                        "submitted_solution": solution.strip()
                    }
                )
                
                if response.status_code != 200:
                    logger.error(f"API call failed with status {response.status_code}: {response.text}")
                    
                    # Handle specific error cases
                    if response.status_code == 404:
                        if "not a member of any squad" in response.text:
                            content = "❌ You must be a member of a squad to submit solutions. Use `/squads join` to join a squad first."
                        else:
                            content = "❌ Challenge not found or not available yet."
                    elif response.status_code == 403:
                        content = "❌ Challenge has not been released yet. Please wait for the challenge to be announced."
                    else:
                        content = "❌ Failed to submit solution. Please try again later."
                    
                    await interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_CREATE,
                        content=content,
                        flags=hikari.MessageFlag.EPHEMERAL
                    )
                    return
                
                # Parse the API response
                response_data = response.json()
                is_correct = response_data.get("is_correct", False)
                is_first_success = response_data.get("is_first_success", False)
                points_earned = response_data.get("points_earned")
                challenge_info = response_data.get("challenge", {})
                squad_info = response_data.get("squad", {})
                
                # Create response message based on correctness
                if is_correct:
                    if is_first_success:
                        # First correct solution for the squad
                        points_line = f"🎯 **Points Earned:** {points_earned}\n" if points_earned is not None else ""
                        content = (
                            f"🎉 **Correct Solution!**\n\n"
                            f"**Challenge:** {challenge_info.get('title', self.challenge_title)}\n"
                            f"**Squad:** {squad_info.get('name', 'Your Squad')}\n\n"
                            f"🏆 **This is your squad's first correct solution!** Great work!\n\n"
                            f"{points_line}"
                            f"Your solution has been recorded with a timestamp."
                        )
                        flags = hikari.MessageFlag.NONE  # Public message for celebration
                    else:
                        # Correct but not the first
                        content = (
                            f"✅ **Correct Solution!**\n\n"
                            f"**Challenge:** {challenge_info.get('title', self.challenge_title)}\n"
                            f"**Squad:** {squad_info.get('name', 'Your Squad')}\n\n"
                            f"Your solution is correct! Your squad has already successfully solved this challenge."
                        )
                        flags = hikari.MessageFlag.EPHEMERAL  # Private message
                else:
                    # Incorrect solution
                    content = (
                        f"❌ **Incorrect Solution**\n\n"
                        f"**Challenge:** {challenge_info.get('title', self.challenge_title)}\n"
                        f"**Squad:** {squad_info.get('name', 'Your Squad')}\n\n"
                        f"Your solution is not correct. Please review the challenge and try again."
                    )
                    flags = hikari.MessageFlag.EPHEMERAL  # Private message
                
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content=content,
                    flags=flags
                )
                
                logger.info(
                    f"Solution submission completed for challenge {self.challenge_id} by user {self.user.id}: "
                    f"correct={is_correct}, first_success={is_first_success}"
                )
                
            except APIError as api_error:
                logger.error(f"API call failed while submitting solution: {api_error}")
                
                # Handle specific API error cases based on status code and message
                error_message = str(api_error)
                status_code = getattr(api_error, 'status_code', None)
                
                if status_code == 404:
                    if "not a member of any squad" in error_message:
                        content = "❌ You must be a member of a squad to submit solutions. Use `/squads join` to join a squad first."
                    else:
                        content = "❌ Challenge not found or not available yet."
                elif status_code == 403:
                    content = "❌ Challenge has not been released yet. Please wait for the challenge to be announced."
                else:
                    content = f"❌ Failed to submit solution: {error_message}"
                
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content=content,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            except Exception as api_error:
                logger.exception(f"Unexpected error while submitting solution: {api_error}")
                
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Failed to submit solution. Please try again later.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
        
        except Exception as e:
            logger.exception(f"Unexpected error in solution submission modal: {e}")
            
            try:
                if not interaction.is_responded():
                    await interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_CREATE,
                        content="❌ An unexpected error occurred while submitting your solution. Please try again later.",
                        flags=hikari.MessageFlag.EPHEMERAL
                    )
            except Exception as e2:
                logger.error(f"Failed to send error response: {e2}")