"""Interactive squad selection views for Discord bot.

This module provides interactive UI components for squad selection and
management, using Discord's select menu components.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import UUID

import hikari

from smarter_dev.bot.utils.embeds import create_success_embed
from smarter_dev.bot.utils.image_embeds import get_generator

if TYPE_CHECKING:
    from smarter_dev.bot.services.models import Squad
    from smarter_dev.bot.services.squads_service import SquadsService

logger = logging.getLogger(__name__)


async def _send_squad_join_announcement(
    bot,
    guild_id: str,
    user_id: str,
    squad,
    announcement_channel_id: str
) -> None:
    """Send a squad join announcement to the squad's announcement channel.

    Args:
        bot: The Discord bot instance
        guild_id: Discord guild ID
        user_id: User ID who joined the squad
        squad: Squad object with squad information
        announcement_channel_id: Channel ID to send announcement to
    """
    try:
        # Only announce for non-default squads
        if getattr(squad, "is_default", False):
            logger.debug(f"Skipping announcement for default squad {squad.name}")
            return

        # Get user info for display name
        try:
            user = await bot.rest.fetch_user(int(user_id))
            display_name = user.display_name or user.username
        except Exception as e:
            logger.warning(f"Could not fetch user {user_id} for announcement: {e}")
            display_name = f"User {user_id}"

        # Create green success embed announcement
        generator = get_generator()

        # Create announcement message
        announcement_title = "New Squad Member!"
        announcement_description = f"{display_name} has joined {squad.name}!"

        # Create the green embed image
        image_file = generator.create_success_embed(announcement_title, announcement_description)

        # Send the announcement
        await bot.rest.create_message(
            channel=int(announcement_channel_id),
            attachment=image_file
        )

        logger.info(f"Successfully sent squad join announcement for {display_name} joining {squad.name}")

    except Exception as e:
        logger.error(f"Failed to send squad join announcement for user {user_id} joining squad {squad.name}: {e}")
        # Don't raise - we don't want squad joins to fail because of announcement issues


class SquadSelectView:
    """Interactive squad selection view using Discord select menus.

    This view handles the interactive squad selection process, including
    cost validation, user feedback, and squad joining operations.
    """

    def __init__(
        self,
        squads: list[Squad],
        current_squad: Squad | None,
        user_balance: int,
        user_id: str,
        guild_id: str,
        squads_service: SquadsService,
        timeout: int = 60
    ):
        """Initialize the squad selection view.

        Args:
            squads: List of available squads
            current_squad: User's current squad (if any)
            user_balance: User's current bytes balance
            user_id: Discord user ID
            guild_id: Discord guild ID
            squads_service: Squad service for join operations
            timeout: View timeout in seconds
        """
        self.squads = squads
        self.current_squad = current_squad
        self.user_balance = user_balance
        self.user_id = user_id
        self.guild_id = guild_id
        self.squads_service = squads_service
        self.timeout = timeout
        self.selected_squad_id: UUID | None = None
        self._response = None
        self._timeout_task = None
        self._is_processing = False
        self._bot = None

    def build(self) -> list[hikari.api.ActionRowBuilder]:
        """Build the select menu components.

        Returns:
            List of action row builders containing the select menu
        """
        # Create select menu options (skip default squads)
        options = []
        for squad in self.squads[:25]:  # Discord select menu limit
            # Skip default squads - they cannot be manually joined
            if getattr(squad, "is_default", False):
                continue
            # Calculate join cost (use sale-discounted cost if available)
            switch_cost = squad.current_join_cost if hasattr(squad, "current_join_cost") else squad.switch_cost
            # Skip showing cost if user is already in this squad
            if self.current_squad and self.current_squad.id == squad.id:
                switch_cost = 0

            # Check if user can afford
            can_afford = self.user_balance >= switch_cost

            # Create option label
            label = squad.name
            if len(label) > 100:  # Discord limit
                label = label[:97] + "..."

            # Create description
            if self.current_squad and self.current_squad.id == squad.id:
                description = "Your current squad"
            elif squad.is_default:
                description = "🏠 Default squad - Auto-assigned when earning bytes"
            elif switch_cost > 0:
                # Check if it's on sale
                has_sale = hasattr(squad, "has_join_sale") and squad.has_join_sale
                sale_suffix = " (Sale)" if has_sale else ""

                if can_afford:
                    description = f"Cost: {switch_cost:,} bytes{sale_suffix}"
                else:
                    description = f"⚠️ Need {switch_cost:,} bytes{sale_suffix} (you have {self.user_balance:,})"
            else:
                description = "Free to join!"

            # Limit description length
            if len(description) > 100:
                description = description[:97] + "..."

            # Skip emoji to avoid Discord validation issues

            option = hikari.SelectMenuOption(
                label=label,
                value=str(squad.id),
                description=description,
                emoji=hikari.UNDEFINED,
                is_default=self.current_squad and self.current_squad.id == squad.id
            )
            options.append(option)

        # Build action row
        action_row = hikari.impl.MessageActionRowBuilder()
        select_menu = action_row.add_text_menu(
            "squad_select",
            placeholder="Choose a squad to join...",
            min_values=1,
            max_values=1
        )

        # Add options to the select menu
        for option in options:
            select_menu.add_option(
                option.label,
                option.value,
                description=option.description,
                is_default=option.is_default
            )

        return [action_row]

    def start(self, response, bot=None) -> None:
        """Start the view with timeout handling.

        Args:
            response: The Discord response object to edit
            bot: The bot instance for registering the view
        """
        self._response = response
        self._bot = bot

        # Register this view with the bot for interaction handling
        if bot:
            if not hasattr(bot, "d"):
                bot.d = {}
            if "active_views" not in bot.d:
                bot.d["active_views"] = {}

            view_key = f"{self.user_id}_squad"
            bot.d["active_views"][view_key] = self

            logger.info(f"Registered squad select view for user {self.user_id}")

        # Only create timeout task if there's a running event loop
        try:
            loop = asyncio.get_running_loop()

            # Set up timeout
            async def timeout_handler():
                await asyncio.sleep(self.timeout)
                if not self._is_processing and self.selected_squad_id is None:
                    await self._handle_timeout()

            self._timeout_task = loop.create_task(timeout_handler())
        except RuntimeError:
            # No running event loop (likely in tests)
            self._timeout_task = None

    async def handle_interaction(self, event: hikari.InteractionCreateEvent) -> None:
        """Handle select menu interaction.

        Args:
            event: The interaction event
        """
        if not isinstance(event.interaction, hikari.ComponentInteraction):
            return

        if event.interaction.custom_id != "squad_select":
            return

        # Prevent multiple processing
        if self._is_processing:
            try:
                # Try to defer first
                try:
                    await event.interaction.create_initial_response(
                        hikari.ResponseType.DEFERRED_MESSAGE_UPDATE
                    )
                    generator = get_generator()
                    image_file = generator.create_error_embed("Processing your previous selection...")
                    await event.interaction.edit_initial_response(
                        attachment=image_file,
                        components=[]
                    )
                except Exception:
                    # If defer fails, try direct response
                    try:
                        generator = get_generator()
                        image_file = generator.create_error_embed("Processing your previous selection...")
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_UPDATE,
                            attachment=image_file,
                            components=[]
                        )
                    except Exception:
                        # Give up gracefully
                        pass
            except Exception:
                # Interaction may already be deferred/responded to
                pass
            return

        self._is_processing = True

        # Cancel timeout
        if self._timeout_task:
            self._timeout_task.cancel()

        try:
            # Defer the interaction first to prevent timeout
            try:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.DEFERRED_MESSAGE_UPDATE
                )
            except Exception as defer_error:
                logger.warning(f"Failed to defer interaction: {defer_error}")
                # If we can't defer, the interaction may have already expired
                # Try to send a follow-up message instead
                try:
                    generator = get_generator()
                    image_file = generator.create_error_embed("This interaction has expired. Please try the command again.")
                    await event.interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_UPDATE,
                        attachment=image_file,
                        components=[]
                    )
                except Exception:
                    # Give up gracefully
                    pass
                return

            # Get selected squad ID
            selected_value = event.interaction.values[0]
            self.selected_squad_id = UUID(selected_value)

            # Find selected squad
            selected_squad = next((s for s in self.squads if s.id == self.selected_squad_id), None)
            if not selected_squad:
                generator = get_generator()
                image_file = generator.create_error_embed("Selected squad not found!")
                await event.interaction.edit_initial_response(
                    attachment=image_file,
                    components=[]
                )
                return

            # Check if user is already in this squad
            if self.current_squad and self.current_squad.id == selected_squad.id:
                generator = get_generator()
                image_file = generator.create_error_embed(f"You're already in the {selected_squad.name} squad!")
                await event.interaction.edit_initial_response(
                    attachment=image_file,
                    components=[]
                )
                return

            # Process squad join
            # Get username for transaction records
            username = None
            try:
                # Try to get user from the interaction context
                if hasattr(event.interaction, "user"):
                    username = event.interaction.user.display_name or event.interaction.user.username
            except:
                pass  # Fall back to None

            result = await self.squads_service.join_squad(
                self.guild_id,
                self.user_id,
                self.selected_squad_id,
                self.user_balance,
                username
            )

            if not result.success:
                generator = get_generator()
                image_file = generator.create_error_embed(result.reason)
            else:
                # Assign Discord role for the new squad
                try:
                    # Get the Discord guild and member
                    guild = event.interaction.get_guild()
                    if guild:
                        member = guild.get_member(int(self.user_id))
                        if member and result.squad.role_id:
                            # Remove previous squad role if switching squads
                            if result.previous_squad and result.previous_squad.role_id:
                                try:
                                    await member.remove_role(int(result.previous_squad.role_id))
                                    logger.info(f"Removed role {result.previous_squad.role_id} from user {self.user_id}")
                                except Exception as e:
                                    logger.warning(f"Failed to remove previous squad role {result.previous_squad.role_id}: {e}")

                            # Add new squad role
                            try:
                                await member.add_role(int(result.squad.role_id))
                                logger.info(f"Assigned role {result.squad.role_id} to user {self.user_id}")
                            except Exception as e:
                                f"\n⚠️ Role assignment failed: {str(e)}"
                                logger.error(f"Failed to assign squad role {result.squad.role_id} to user {self.user_id}: {e}")
                        else:
                            logger.warning(f"Could not find member {self.user_id} in guild or squad has no role_id")
                    else:
                        logger.warning("Could not get guild from interaction")
                except Exception as e:
                    f"\n⚠️ Role assignment error: {str(e)}"
                    logger.error(f"Error during role assignment for user {self.user_id}: {e}")

                # Send announcement to squad's announcement channel if configured
                if hasattr(result.squad, "announcement_channel") and result.squad.announcement_channel:
                    try:
                        await _send_squad_join_announcement(
                            self._bot,
                            self.guild_id,
                            self.user_id,
                            result.squad,
                            result.squad.announcement_channel
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send squad join announcement: {e}")

                # Build clean success description with custom welcome message
                if result.squad.welcome_message:
                    description = result.squad.welcome_message
                else:
                    description = f"Welcome to {result.squad.name}! We're glad to have you aboard."

                generator = get_generator()
                image_file = generator.create_success_embed("SQUAD JOINED", description)

        except Exception as e:
            logger.exception(f"Error processing squad selection: {e}")
            generator = get_generator()
            image_file = generator.create_error_embed(f"Failed to join squad: {str(e)}")

        try:
            await event.interaction.edit_initial_response(
                attachment=image_file,
                components=[]
            )

            # Clean up view registration after successful interaction
            if self._bot and hasattr(self._bot, "d") and "active_views" in self._bot.d:
                view_key = f"{self.user_id}_squad"
                self._bot.d["active_views"].pop(view_key, None)
                logger.info(f"Cleaned up completed view for user {self.user_id}")

        except Exception as e:
            logger.error(f"Failed to update interaction response: {e}")

    async def _handle_timeout(self) -> None:
        """Handle view timeout."""
        # Clean up view registration
        if self._bot and hasattr(self._bot, "d") and "active_views" in self._bot.d:
            view_key = f"{self.user_id}_squad"
            self._bot.d["active_views"].pop(view_key, None)
            logger.info(f"Cleaned up timed out view for user {self.user_id}")

        if self._response:
            try:
                generator = get_generator()
                image_file = generator.create_simple_embed(
                    "SQUAD SELECTION TIMED OUT",
                    "You took too long to choose a squad. Please try the command again.",
                    "warning"
                )
                await self._response.edit(attachment=image_file, components=[])
            except Exception as e:
                logger.error(f"Failed to handle timeout: {e}")


class SquadConfirmView:
    """Confirmation view for squad operations.

    This view provides a simple yes/no confirmation for squad operations
    that require user confirmation.
    """

    def __init__(
        self,
        title: str,
        description: str,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        timeout: int = 30
    ):
        """Initialize the confirmation view.

        Args:
            title: Confirmation dialog title
            description: Confirmation dialog description
            confirm_label: Label for confirm button
            cancel_label: Label for cancel button
            timeout: View timeout in seconds
        """
        self.title = title
        self.description = description
        self.confirm_label = confirm_label
        self.cancel_label = cancel_label
        self.timeout = timeout
        self.confirmed: bool | None = None
        self._response = None
        self._timeout_task = None

    def build(self) -> list[hikari.api.ActionRowBuilder]:
        """Build the confirmation buttons.

        Returns:
            List of action row builders containing the buttons
        """
        action_row = hikari.impl.MessageActionRowBuilder()

        # Confirm button
        action_row.add_interactive_button(
            hikari.ButtonStyle.SUCCESS,
            "squad_confirm",
            label=self.confirm_label
        )

        # Cancel button
        action_row.add_interactive_button(
            hikari.ButtonStyle.SECONDARY,
            "squad_cancel",
            label=self.cancel_label
        )

        return [action_row]

    def start(self, response) -> None:
        """Start the view with timeout handling.

        Args:
            response: The Discord response object to edit
        """
        self._response = response

        # Only create timeout task if there's a running event loop
        try:
            loop = asyncio.get_running_loop()

            # Set up timeout
            async def timeout_handler():
                await asyncio.sleep(self.timeout)
                if self.confirmed is None:
                    await self._handle_timeout()

            self._timeout_task = loop.create_task(timeout_handler())
        except RuntimeError:
            # No running event loop (likely in tests)
            self._timeout_task = None

    async def handle_interaction(self, event: hikari.InteractionCreateEvent) -> None:
        """Handle button interaction.

        Args:
            event: The interaction event
        """
        if not isinstance(event.interaction, hikari.ComponentInteraction):
            return

        if event.interaction.custom_id not in ["squad_confirm", "squad_cancel"]:
            return

        # Cancel timeout
        if self._timeout_task:
            self._timeout_task.cancel()

        # Set result
        self.confirmed = event.interaction.custom_id == "squad_confirm"

        # Create response embed
        if self.confirmed:
            embed = create_success_embed("✅ Confirmed", "Operation confirmed!")
        else:
            embed = hikari.Embed(
                title="❌ Cancelled",
                description="Operation cancelled.",
                color=hikari.Color(0x6b7280)
            )

        try:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                embed=embed,
                components=[]
            )
        except Exception as e:
            logger.error(f"Failed to update confirmation response: {e}")

    async def _handle_timeout(self) -> None:
        """Handle view timeout."""
        if self._response:
            try:
                embed = hikari.Embed(
                    title="⏰ Confirmation Timed Out",
                    description="You took too long to respond. Operation cancelled.",
                    color=hikari.Color(0xf59e0b)
                )
                await self._response.edit(embed=embed, components=[])
            except Exception as e:
                logger.error(f"Failed to handle confirmation timeout: {e}")

    async def wait(self) -> bool:
        """Wait for user confirmation.

        Returns:
            True if confirmed, False if cancelled or timed out
        """
        # Wait for timeout task to complete or be cancelled
        if self._timeout_task:
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass

        return self.confirmed is True


class SquadListShareView:
    """View with share button for squad list command."""

    def __init__(self, squads: list[Squad], guild_name: str, current_squad_id: str = None, guild_roles: dict = None, has_active_campaign: bool = False):
        """Initialize the squad list share view.

        Args:
            squads: List of squad objects
            guild_name: Name of the guild
            current_squad_id: ID of user's current squad (if any)
            guild_roles: Dictionary mapping role IDs to colors
            has_active_campaign: Whether there's an active campaign
        """
        self.squads = squads
        self.guild_name = guild_name
        self.current_squad_id = current_squad_id
        self.guild_roles = guild_roles or {}
        self.has_active_campaign = has_active_campaign
        self._timeout = 300  # 5 minutes

    def build_components(self) -> list[hikari.api.ComponentBuilder]:
        """Build the action row components."""
        share_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.PRIMARY,
            custom_id="share_squad_list",
            emoji="📤",
            label="Share"
        )

        action_row = hikari.impl.MessageActionRowBuilder()
        action_row.add_component(share_button)
        return [action_row]
