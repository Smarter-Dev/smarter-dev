"""Interactive squad selection views for Discord bot.

This module provides interactive UI components for squad selection and
management, using Discord's select menu components.
"""

from __future__ import annotations

import hikari
import logging
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID
import asyncio

from smarter_dev.bot.utils.embeds import create_error_embed, create_success_embed

if TYPE_CHECKING:
    from smarter_dev.bot.services.squads_service import SquadsService
    from smarter_dev.bot.services.models import Squad

logger = logging.getLogger(__name__)


class SquadSelectView:
    """Interactive squad selection view using Discord select menus.
    
    This view handles the interactive squad selection process, including
    cost validation, user feedback, and squad joining operations.
    """
    
    def __init__(
        self,
        squads: List[Squad],
        current_squad: Optional[Squad],
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
        self.selected_squad_id: Optional[UUID] = None
        self._response = None
        self._timeout_task = None
        self._is_processing = False
        
    def build(self) -> List[hikari.api.ActionRowBuilder]:
        """Build the select menu components.
        
        Returns:
            List of action row builders containing the select menu
        """
        # Create select menu options
        options = []
        for squad in self.squads[:25]:  # Discord select menu limit
            # Calculate switch cost
            switch_cost = 0
            if self.current_squad and self.current_squad.id != squad.id:
                switch_cost = squad.switch_cost
            
            # Check if user can afford
            can_afford = self.user_balance >= switch_cost
            
            # Create option label
            label = squad.name
            if len(label) > 100:  # Discord limit
                label = label[:97] + "..."
            
            # Create description
            if switch_cost > 0:
                if can_afford:
                    description = f"Cost: {switch_cost:,} bytes"
                else:
                    description = f"⚠️ Need {switch_cost:,} bytes (you have {self.user_balance:,})"
            else:
                description = "Free to join!"
            
            # Limit description length
            if len(description) > 100:
                description = description[:97] + "..."
            
            # Emoji for current squad
            emoji = "✅" if self.current_squad and self.current_squad.id == squad.id else None
            
            option = hikari.SelectMenuOption(
                label=label,
                value=str(squad.id),
                description=description,
                emoji=emoji,
                is_default=self.current_squad and self.current_squad.id == squad.id
            )
            options.append(option)
        
        # Build action row
        action_row = hikari.impl.ActionRowBuilder()
        action_row.add_select_menu(
            hikari.ComponentType.TEXT_SELECT_MENU,
            custom_id="squad_select",
            options=options,
            placeholder="Choose a squad to join...",
            min_values=1,
            max_values=1
        )
        
        return [action_row]
    
    def start(self, response) -> None:
        """Start the view with timeout handling.
        
        Args:
            response: The Discord response object to edit
        """
        self._response = response
        
        # Set up timeout
        async def timeout_handler():
            await asyncio.sleep(self.timeout)
            if not self._is_processing and self.selected_squad_id is None:
                await self._handle_timeout()
        
        self._timeout_task = asyncio.create_task(timeout_handler())
    
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
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                embed=create_error_embed("Processing your previous selection..."),
                components=[]
            )
            return
        
        self._is_processing = True
        
        # Cancel timeout
        if self._timeout_task:
            self._timeout_task.cancel()
        
        try:
            # Get selected squad ID
            selected_value = event.interaction.values[0]
            self.selected_squad_id = UUID(selected_value)
            
            # Find selected squad
            selected_squad = next((s for s in self.squads if s.id == self.selected_squad_id), None)
            if not selected_squad:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_UPDATE,
                    embed=create_error_embed("Selected squad not found!"),
                    components=[]
                )
                return
            
            # Check if user is already in this squad
            if self.current_squad and self.current_squad.id == selected_squad.id:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_UPDATE,
                    embed=create_error_embed(f"You're already in the {selected_squad.name} squad!"),
                    components=[]
                )
                return
            
            # Process squad join
            result = await self.squads_service.join_squad(
                self.guild_id,
                self.user_id,
                self.selected_squad_id,
                self.user_balance
            )
            
            if not result.success:
                embed = hikari.Embed(
                    title="❌ Join Failed",
                    description=result.reason,
                    color=hikari.Color(0xef4444)
                )
            else:
                embed = hikari.Embed(
                    title="✅ Squad Joined!",
                    description=f"You've successfully joined **{result.squad.name}**!",
                    color=hikari.Color(0x22c55e)
                )
                
                if result.cost and result.cost > 0:
                    embed.add_field("Cost Paid", f"**{result.cost:,}** bytes", inline=True)
                    if result.new_balance is not None:
                        embed.add_field("New Balance", f"**{result.new_balance:,}** bytes", inline=True)
                
                if result.previous_squad:
                    embed.add_field("Previous Squad", result.previous_squad.name, inline=True)
        
        except Exception as e:
            logger.exception(f"Error processing squad selection: {e}")
            embed = hikari.Embed(
                title="❌ Error",
                description=f"Failed to join squad: {str(e)}",
                color=hikari.Color(0xef4444)
            )
        
        try:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                embed=embed,
                components=[]
            )
        except Exception as e:
            logger.error(f"Failed to update interaction response: {e}")
    
    async def _handle_timeout(self) -> None:
        """Handle view timeout."""
        if self._response:
            try:
                embed = hikari.Embed(
                    title="⏰ Squad Selection Timed Out",
                    description="You took too long to choose a squad. Please try the command again.",
                    color=hikari.Color(0xf59e0b)
                )
                await self._response.edit(embed=embed, components=[])
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
        self.confirmed: Optional[bool] = None
        self._response = None
        self._timeout_task = None
    
    def build(self) -> List[hikari.api.ActionRowBuilder]:
        """Build the confirmation buttons.
        
        Returns:
            List of action row builders containing the buttons
        """
        action_row = hikari.impl.ActionRowBuilder()
        
        # Confirm button
        action_row.add_button(
            hikari.ButtonStyle.SUCCESS,
            custom_id="squad_confirm",
            label=self.confirm_label,
            emoji="✅"
        )
        
        # Cancel button
        action_row.add_button(
            hikari.ButtonStyle.SECONDARY,
            custom_id="squad_cancel",
            label=self.cancel_label,
            emoji="❌"
        )
        
        return [action_row]
    
    def start(self, response) -> None:
        """Start the view with timeout handling.
        
        Args:
            response: The Discord response object to edit
        """
        self._response = response
        
        # Set up timeout
        async def timeout_handler():
            await asyncio.sleep(self.timeout)
            if self.confirmed is None:
                await self._handle_timeout()
        
        self._timeout_task = asyncio.create_task(timeout_handler())
    
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