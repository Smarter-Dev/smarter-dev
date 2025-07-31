"""Interactive bytes transfer modal for Discord bot.

This module provides modal components for bytes transfer operations,
including amount and reason input with validation.
"""

from __future__ import annotations

import hikari
import logging
from typing import TYPE_CHECKING, Optional

from smarter_dev.bot.utils.image_embeds import get_generator
from smarter_dev.bot.services.exceptions import (
    InsufficientBalanceError,
    ServiceError,
    ValidationError
)

if TYPE_CHECKING:
    from smarter_dev.bot.services.bytes_service import BytesService

logger = logging.getLogger(__name__)


def create_send_bytes_modal(
    recipient: hikari.User,
    max_transfer: int
) -> hikari.api.InteractionModalBuilder:
    """Create a send bytes modal.
    
    Args:
        recipient: User receiving the bytes
        max_transfer: Maximum transfer amount for this guild
        
    Returns:
        The modal builder instance
    """
    modal = hikari.impl.InteractionModalBuilder(
        title=f"Send Bytes to {recipient.username}",
        custom_id=f"send_bytes_modal:{recipient.id}"
    )
    
    # Add amount text input
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="amount",
                label="Amount",
                placeholder=f"Enter amount (1-{max_transfer:,})",
                required=True,
                min_length=1,
                max_length=10,
                style=hikari.TextInputStyle.SHORT
            )
        )
    )
    
    # Add reason text input
    modal.add_component(
        hikari.impl.ModalActionRowBuilder().add_component(
            hikari.impl.TextInputBuilder(
                custom_id="reason",
                label="Reason (Optional)",
                placeholder="Why are you sending these bytes?",
                required=False,
                max_length=200,
                style=hikari.TextInputStyle.PARAGRAPH
            )
        )
    )
    return modal


class SendBytesModalHandler:
    """Handler for send bytes modal interactions."""
    
    def __init__(
        self,
        recipient: hikari.User,
        guild_id: str,
        giver: hikari.User,
        max_transfer: int,
        bytes_service: BytesService,
        target_message_id: Optional[int] = None
    ):
        """Initialize the modal handler.
        
        Args:
            recipient: User receiving the bytes
            guild_id: Discord guild ID
            giver: User sending the bytes
            max_transfer: Maximum transfer amount for this guild
            bytes_service: Bytes service for transfer operations
            target_message_id: Optional message ID to reply to (for context menu)
        """
        self.recipient = recipient
        self.guild_id = guild_id
        self.giver = giver
        self.max_transfer = max_transfer
        self.bytes_service = bytes_service
        self.target_message_id = target_message_id
    
    async def handle_submit(self, interaction: hikari.ModalInteraction) -> None:
        """Handle modal submission and process the bytes transfer.
        
        Args:
            interaction: The modal interaction
        """
        try:
            # Get the input values
            amount_str = None
            reason = None
            
            for component in interaction.components:
                if hasattr(component, 'components'):
                    for text_input in component.components:
                        if text_input.custom_id == "amount":
                            amount_str = text_input.value
                        elif text_input.custom_id == "reason":
                            reason = text_input.value if text_input.value else None
            
            if not amount_str:
                generator = get_generator()
                image_file = generator.create_error_embed("Amount is required.")
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return
            
            # Validate amount
            try:
                amount = int(amount_str)
            except ValueError:
                generator = get_generator()
                image_file = generator.create_error_embed("Amount must be a valid number.")
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return
            
            # Validate amount range
            if amount < 1:
                generator = get_generator()
                image_file = generator.create_error_embed("Amount must be at least 1 byte.")
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return
            
            if amount > self.max_transfer:
                generator = get_generator()
                image_file = generator.create_error_embed(
                    f"Amount cannot exceed {self.max_transfer:,} bytes (server limit)."
                )
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return
            
            # Process the transfer
            logger.info(f"Processing bytes transfer: {self.giver} -> {self.recipient}, amount: {amount}")
            
            result = await self.bytes_service.transfer_bytes(
                guild_id=self.guild_id,
                giver=self.giver,
                receiver=self.recipient,
                amount=amount,
                reason=reason
            )
            
            generator = get_generator()
            
            if result.success:
                # Create success embed with same format as slash command
                description = f"{str(self.giver)} sent {amount:,} bytes to {str(self.recipient)}"
                
                # Add reason if provided
                if reason:
                    description += f"\n\n{reason}"
                
                image_file = generator.create_success_embed("BYTES SENT", description)
                logger.info(f"✅ Transfer successful: {amount} bytes from {self.giver} to {self.recipient}")
            else:
                # Use special cooldown embed for cooldown errors
                if result.is_cooldown_error:
                    logger.info("Creating cooldown image embed")
                    image_file = generator.create_cooldown_embed(result.reason, result.cooldown_end_timestamp)
                else:
                    # Use error embed for transfer limit and other errors
                    logger.info("Creating error image embed")
                    image_file = generator.create_error_embed(result.reason)
            
            logger.info(f"Created image file: {type(image_file)}")
            
            if result.success:
                # Success messages should be public for everyone to see
                if self.target_message_id:
                    # For context menu: Defer response then create reply message
                    await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
                    
                    # Create a reply message to the original message
                    await interaction.app.rest.create_message(
                        interaction.channel_id,
                        attachment=image_file,
                        reply=self.target_message_id
                    )
                    
                    # Delete the deferred response since we created our own message
                    await interaction.delete_initial_response()
                else:
                    # For slash command: Regular response
                    await interaction.create_initial_response(
                        hikari.ResponseType.MESSAGE_CREATE,
                        attachment=image_file
                    )
            else:
                # Error messages should be private (ephemeral)
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            
        except InsufficientBalanceError as e:
            logger.info(f"Insufficient balance for transfer: {e}")
            generator = get_generator()
            image_file = generator.create_error_embed(str(e))
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                attachment=image_file,
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except ValidationError as e:
            logger.info(f"Validation error in transfer: {e}")
            generator = get_generator()
            image_file = generator.create_error_embed(str(e))
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                attachment=image_file,
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except ServiceError as e:
            logger.error(f"Service error in transfer: {e}")
            generator = get_generator()
            image_file = generator.create_error_embed("Transfer failed. Please try again later.")
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                attachment=image_file,
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except Exception as e:
            logger.exception(f"Unexpected error in bytes transfer modal: {e}")
            generator = get_generator()
            image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                attachment=image_file,
                flags=hikari.MessageFlag.EPHEMERAL
            )