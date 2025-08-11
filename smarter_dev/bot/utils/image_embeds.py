"""Image-based Discord embed generator using Pillow.

This module provides utilities for creating Discord embeds as images rather than
text, using Pillow for image composition and text rendering. Supports custom
backgrounds, fonts, and color schemes for consistent visual branding.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Union

import hikari
from PIL import Image, ImageDraw, ImageFont

from smarter_dev.bot.services.models import BytesBalance


class EmbedImageGenerator:
    """Generator for Discord embed images using Pillow.
    
    Features:
    - Custom background images for different embed types
    - Multiple font families with proper sizing
    - Color-coded titles based on embed type
    - Proper padding and text wrapping
    - Support for complex layouts
    """
    
    # Color schemes for different embed types
    COLORS = {
        "default": "#00E1FF",  # Cyan
        "error": "#FF0004",    # Red
        "success": "#11FF00",  # Green
        "warning": "#f59e0b",  # Amber
        "info": "#3b82f6"      # Blue
    }
    
    # Text color
    TEXT_COLOR = "#FFFFFF"  # White
    
    # Padding configuration
    PADDING_TOP = 64
    PADDING_HORIZONTAL = 64
    PADDING_BOTTOM = 32
    
    def __init__(self, resources_path: Optional[Union[str, Path]] = None):
        """Initialize the image generator.
        
        Args:
            resources_path: Path to resources directory. If None, uses default.
        """
        if resources_path is None:
            # Default to resources directory relative to this file
            current_dir = Path(__file__).parent.parent.parent.parent
            resources_path = current_dir / "resources"
        
        self.resources_path = Path(resources_path)
        self.embeds_path = self.resources_path / "discord-embeds"
        self.fonts_path = self.resources_path / "fonts"
        
        # Font cache
        self._fonts = {}
        
        # Load fonts on initialization
        self._load_fonts()
    
    def _load_fonts(self) -> None:
        """Load fonts into memory for reuse."""
        try:
            # Bruno Ace SC for titles - thicker, more readable on mobile
            bruno_path = self.fonts_path / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf"
            # Anta Regular for body text - friendly readable font
            anta_path = self.fonts_path / "Anta" / "Anta-Regular.ttf"
            
            if bruno_path.exists() and anta_path.exists():
                # Title fonts - using Bruno Ace SC
                self._fonts["title_large"] = ImageFont.truetype(str(bruno_path), 60)
                self._fonts["title_medium"] = ImageFont.truetype(str(bruno_path), 48)
                self._fonts["title_small"] = ImageFont.truetype(str(bruno_path), 36)
                
                # Body text fonts - using Anta Regular
                self._fonts["text_large"] = ImageFont.truetype(str(anta_path), 32)
                self._fonts["text_medium"] = ImageFont.truetype(str(anta_path), 28)
                self._fonts["text_small"] = ImageFont.truetype(str(anta_path), 24)
                self._fonts["text_tiny"] = ImageFont.truetype(str(anta_path), 20)
            else:
                # Fall back to Bruno for everything if Anta is missing
                if bruno_path.exists():
                    self._fonts["title_large"] = ImageFont.truetype(str(bruno_path), 60)
                    self._fonts["title_medium"] = ImageFont.truetype(str(bruno_path), 48)
                    self._fonts["title_small"] = ImageFont.truetype(str(bruno_path), 36)
                    self._fonts["text_large"] = ImageFont.truetype(str(bruno_path), 32)
                    self._fonts["text_medium"] = ImageFont.truetype(str(bruno_path), 28)
                    self._fonts["text_small"] = ImageFont.truetype(str(bruno_path), 24)
                    self._fonts["text_tiny"] = ImageFont.truetype(str(bruno_path), 20)
                else:
                    raise Exception("Neither Bruno Ace SC nor Anta fonts found")
            
        except Exception as e:
            # Fall back to default font if custom fonts fail
            print(f"Warning: Could not load custom fonts: {e}")
            default_font = ImageFont.load_default()
            self._fonts = {
                "title_large": default_font,
                "title_medium": default_font,
                "title_small": default_font,
                "text_large": default_font,
                "text_medium": default_font,
                "text_small": default_font,
                "text_tiny": default_font,
            }
    
    def _get_background(self, embed_type: str = "default") -> Image.Image:
        """Load and return the appropriate background image.
        
        Args:
            embed_type: Type of embed (default, error, success, warning, info)
            
        Returns:
            PIL Image object for the background
        """
        background_files = {
            "error": "error-background.png",
            "success": "success-background.png",
            "default": "background.png",
            "warning": "background.png",
            "info": "background.png"
        }
        
        background_file = background_files.get(embed_type, "background.png")
        background_path = self.embeds_path / background_file
        
        try:
            if background_path.exists():
                return Image.open(background_path).convert("RGBA")
            else:
                # Create a simple colored background if file doesn't exist
                return self._create_simple_background()
        except Exception:
            return self._create_simple_background()
    
    def _create_simple_background(self, width: int = 600, height: int = 400) -> Image.Image:
        """Create a simple gradient background as fallback.
        
        Args:
            width: Background width
            height: Background height
            
        Returns:
            PIL Image object
        """
        # Create a simple dark gradient background
        image = Image.new("RGBA", (width, height), (26, 29, 41, 255))
        draw = ImageDraw.Draw(image)
        
        # Add subtle gradient effect
        for y in range(height):
            alpha = int(255 * (1 - y / height * 0.3))
            color = (44, 49, 66, alpha)
            draw.line([(0, y), (width, y)], fill=color)
        
        return image
    
    def _wrap_text_with_spacing(self, text: str, font: ImageFont.ImageFont, max_width: int) -> list[tuple[str, bool]]:
        """Wrap text to fit within specified width, tracking explicit vs wrapped lines.
        
        Args:
            text: Text to wrap
            font: Font to use for measurement
            max_width: Maximum width in pixels
            
        Returns:
            List of (text_line, is_paragraph_break) tuples where is_paragraph_break
            indicates if this line should have extra spacing after it
        """
        # First split by explicit newlines
        paragraphs = text.split('\n')
        lines = []
        
        for paragraph_idx, paragraph in enumerate(paragraphs):
            if not paragraph.strip():
                # Empty line
                lines.append(("", True))
                continue
                
            # Apply word wrapping to each paragraph
            words = paragraph.split()
            current_line = ""
            paragraph_lines = []
            
            for word in words:
                test_line = current_line + (" " if current_line else "") + word
                bbox = font.getbbox(test_line)
                text_width = bbox[2] - bbox[0]
                
                if text_width <= max_width:
                    current_line = test_line
                else:
                    if current_line:
                        paragraph_lines.append(current_line)
                    current_line = word
            
            if current_line:
                paragraph_lines.append(current_line)
            
            # Add paragraph lines with spacing info
            for line_idx, line in enumerate(paragraph_lines):
                # Last line of paragraph gets paragraph break spacing
                is_last_line_of_paragraph = (line_idx == len(paragraph_lines) - 1)
                is_last_paragraph = (paragraph_idx == len(paragraphs) - 1)
                
                # Only add paragraph break spacing if not the last paragraph
                needs_paragraph_spacing = is_last_line_of_paragraph and not is_last_paragraph
                lines.append((line, needs_paragraph_spacing))
        
        return lines
    
    def _draw_text_with_shadow(
        self, 
        draw: ImageDraw.Draw, 
        position: Tuple[int, int], 
        text: str, 
        font: ImageFont.ImageFont, 
        fill: str,
        shadow_offset: Tuple[int, int] = (1, 1),
        shadow_color: str = "#000000"
    ) -> None:
        """Draw text with a subtle shadow effect.
        
        Args:
            draw: ImageDraw object
            position: (x, y) position for text
            text: Text to draw
            font: Font to use
            fill: Text color
            shadow_offset: Shadow offset (x, y)
            shadow_color: Shadow color
        """
        x, y = position
        shadow_x, shadow_y = shadow_offset
        
        # Draw shadow with reduced offset to avoid artifacts
        draw.text((x + shadow_x, y + shadow_y), text, font=font, fill=shadow_color)
        # Draw main text
        draw.text((x, y), text, font=font, fill=fill)
    
    def _draw_colored_circle(
        self,
        draw: ImageDraw.Draw,
        position: Tuple[int, int],
        radius: int,
        color: str
    ) -> None:
        """Draw a colored circle.
        
        Args:
            draw: ImageDraw object
            position: (x, y) center position for circle
            radius: Circle radius in pixels
            color: Hex color string for the circle
        """
        x, y = position
        # Calculate bounding box for circle
        bbox = [x - radius, y - radius, x + radius, y + radius]
        draw.ellipse(bbox, fill=color, outline=None)
    
    def create_simple_embed(
        self, 
        title: str, 
        description: str, 
        embed_type: str = "default"
    ) -> hikari.files.Bytes:
        """Create a simple embed with title and description.
        
        Args:
            title: Embed title
            description: Embed description
            embed_type: Type of embed (default, error, success, warning, info)
            
        Returns:
            hikari.File containing the generated image
        """
        # Load background
        background = self._get_background(embed_type)
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS.get(embed_type, self.COLORS["default"])
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_lines = self._wrap_text_with_spacing(title, title_font, content_width)
        
        for line_text, needs_paragraph_spacing in title_lines:
            if line_text:  # Skip empty lines
                self._draw_text_with_shadow(
                    draw, 
                    (self.PADDING_HORIZONTAL, current_y), 
                    line_text, 
                    title_font, 
                    title_color
                )
            
            # Different spacing for wrapped vs explicit newlines
            line_height = title_font.getbbox(line_text)[3] if line_text else title_font.getbbox("A")[3]
            if needs_paragraph_spacing:
                current_y += line_height + 16  # Extra spacing for paragraph breaks
            else:
                current_y += line_height + 4   # Tight spacing for wrapped lines
        
        # Add spacing between title and description
        current_y += 32
        
        # Draw description - use larger font for better readability
        desc_font = self._fonts["text_large"]
        desc_lines = self._wrap_text_with_spacing(description, desc_font, content_width)
        
        for line_text, needs_paragraph_spacing in desc_lines:
            if line_text:  # Skip empty lines
                self._draw_text_with_shadow(
                    draw, 
                    (self.PADDING_HORIZONTAL, current_y), 
                    line_text, 
                    desc_font, 
                    self.TEXT_COLOR
                )
            
            # Different spacing for wrapped vs explicit newlines
            line_height = desc_font.getbbox(line_text)[3] if line_text else desc_font.getbbox("A")[3]
            if needs_paragraph_spacing:
                current_y += line_height + 12  # Extra spacing for paragraph breaks
            else:
                current_y += line_height + 2   # Tight spacing for wrapped lines
        
        # Keep full background size instead of cropping
        # final_height = current_y + self.PADDING_BOTTOM
        # img = img.crop((0, 0, img.width, final_height))
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_error_embed(self, message: str) -> hikari.File:
        """Create an error embed image.
        
        Args:
            message: Error message to display
            
        Returns:
            hikari.File containing the generated image
        """
        return self.create_simple_embed("ERROR", message, "error")
    
    def create_success_embed(self, title: str, description: str) -> hikari.File:
        """Create a success embed image.
        
        Args:
            title: Success message title
            description: Success message description
            
        Returns:
            hikari.File containing the generated image
        """
        return self.create_simple_embed(title, description, "success")
    
    def create_info_embed(self, title: str, description: str) -> hikari.File:
        """Create an info embed image.
        
        Args:
            title: Info message title
            description: Info message description
            
        Returns:
            hikari.File containing the generated image
        """
        return self.create_simple_embed(title, description, "info")
    
    def create_cooldown_embed(
        self, 
        message: str, 
        cooldown_end_timestamp: Optional[int] = None
    ) -> hikari.files.Bytes:
        """Create a cooldown-specific embed image.
        
        Args:
            message: Cooldown message to display
            cooldown_end_timestamp: Unix timestamp when cooldown expires
            
        Returns:
            hikari.File containing the generated image
        """
        # Build the description with human-readable time if available
        if cooldown_end_timestamp:
            import time
            current_time = int(time.time())
            time_remaining = cooldown_end_timestamp - current_time
            
            if time_remaining > 0:
                # Format time remaining
                if time_remaining >= 3600:  # 1 hour or more
                    hours = time_remaining // 3600
                    time_str = f"in {hours} hour{'s' if hours != 1 else ''}"
                elif time_remaining >= 60:  # 1 minute or more
                    minutes = time_remaining // 60
                    time_str = f"in {minutes} minute{'s' if minutes != 1 else ''}"
                else:  # Less than 1 minute
                    time_str = f"in {time_remaining} second{'s' if time_remaining != 1 else ''}"
                
                description = f"You can send bytes again {time_str}."
            else:
                description = "You can send bytes again now."
        else:
            description = message
        
        return self.create_simple_embed("TRANSFER COOLDOWN", description, "warning")
    
    def create_leaderboard_embed(
        self, 
        entries: list, 
        guild_name: str, 
        user_display_names: dict
    ) -> hikari.files.Bytes:
        """Create a compact leaderboard embed image with table layout.
        
        Args:
            entries: List of leaderboard entries
            guild_name: Name of the guild
            user_display_names: Mapping of user IDs to display names
            
        Returns:
            hikari.File containing the generated image
        """
        if not entries:
            return self.create_simple_embed("BYTES LEADERBOARD", "No leaderboard data available yet!", "info")
        
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = "BYTES LEADERBOARD"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to content
        current_y += title_font.getbbox(title_text)[3] + 64
        
        # Use smaller font for table content
        table_font = self._fonts["text_small"]
        
        # Calculate the width of the widest ranking number for centering
        max_rank_width = 0
        for entry in entries:
            rank_text = f"{entry.rank}"
            rank_bbox = table_font.getbbox(rank_text)
            rank_width = rank_bbox[2] - rank_bbox[0]
            max_rank_width = max(max_rank_width, rank_width)
        
        # Define rank column width (60px total)
        rank_column_width = 60
        
        # Process leaderboard entries into compact table rows
        for i, entry in enumerate(entries):
            display_name = user_display_names.get(entry.user_id, f"User {entry.user_id[:8]}")
            
            # Truncate username if too long
            if len(display_name) > 18:
                display_name = display_name[:15] + "..."
            
            # Create compact row: Rank | Username | Balance | Streak
            row_y = current_y + (i * 26)  # Adjusted spacing for table rows
            
            # Rank (centered) - special formatting for top 3
            rank_text = f"{entry.rank}"
            rank_color = self.TEXT_COLOR
            if entry.rank == 1:
                rank_color = "#FFD700"  # Gold
            elif entry.rank == 2:
                rank_color = "#C0C0C0"  # Silver  
            elif entry.rank == 3:
                rank_color = "#CD7F32"  # Bronze
            
            # Center the rank number in its column
            rank_bbox = table_font.getbbox(rank_text)
            rank_width = rank_bbox[2] - rank_bbox[0]
            rank_x = self.PADDING_HORIZONTAL + (rank_column_width - rank_width) // 2
            
            draw.text(
                (rank_x, row_y), 
                rank_text, 
                font=table_font, 
                fill=rank_color
            )
            
            # Username (center-left)
            user_x = self.PADDING_HORIZONTAL + 60
            draw.text(
                (user_x, row_y), 
                display_name, 
                font=table_font, 
                fill=self.TEXT_COLOR
            )
            
            # Streak (middle column) - only show if > 0
            if entry.streak_count > 0:
                streak_text = f"{entry.streak_count} days"
                streak_bbox = table_font.getbbox(streak_text)
                streak_width = streak_bbox[2] - streak_bbox[0]
                streak_x = self.PADDING_HORIZONTAL + 400 - streak_width  # Right align in streak column
                
                draw.text(
                    (streak_x, row_y), 
                    streak_text, 
                    font=table_font, 
                    fill="#11FF00"  # Green for streaks
                )
            
            # Balance (right-aligned)
            balance_text = f"{entry.balance:,}"
            balance_bbox = table_font.getbbox(balance_text)
            balance_width = balance_bbox[2] - balance_bbox[0]
            balance_x = self.PADDING_HORIZONTAL + content_width - balance_width
            draw.text(
                (balance_x, row_y), 
                balance_text, 
                font=table_font, 
                fill="#00E1FF"  # Cyan for bytes amounts
            )
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_history_embed(
        self, 
        transactions: list, 
        user_id: str
    ) -> hikari.files.Bytes:
        """Create a compact transaction history embed image with table layout.
        
        Args:
            transactions: List of transactions
            user_id: User ID for filtering perspective
            
        Returns:
            hikari.File containing the generated image
        """
        if not transactions:
            return self.create_simple_embed("TRANSACTION HISTORY", "No transactions found.", "info")
        
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = "TRANSACTION HISTORY"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to content
        current_y += title_font.getbbox(title_text)[3] + 64
        
        # Use smaller font for table content - try text_small instead of text_tiny
        table_font = self._fonts["text_small"]  # 24px instead of 20px to avoid rendering issues
        
        # Process transactions into compact rows
        for i, transaction in enumerate(transactions):
            # Parse date - try different format to avoid rendering issues
            date_str = ""
            if transaction.created_at:
                from datetime import datetime, timezone
                if isinstance(transaction.created_at, str):
                    try:
                        created_dt = datetime.fromisoformat(transaction.created_at.replace('Z', '+00:00'))
                        date_str = created_dt.strftime("%m-%d")  # Use dash instead of slash
                    except:
                        date_str = "N/A"
                else:
                    date_str = transaction.created_at.strftime("%m-%d")
            
            # Determine transaction type and format
            if transaction.giver_id == user_id:
                # User sent bytes
                other_user = transaction.receiver_username
                amount_text = f"-{transaction.amount:,}"
                
                # Special handling for squad join fees and other system transactions
                if transaction.receiver_id == "SYSTEM":
                    type_indicator = "-"  # System deduction
                    if (transaction.reason and 
                        transaction.reason.startswith("Squad join fee:")):
                        # Extract squad name from reason: "Squad join fee: Squad Name"
                        squad_name = transaction.reason.replace("Squad join fee: ", "")
                        other_user = f"Joined {squad_name}"
                    else:
                        other_user = "System Charge"
                else:
                    type_indicator = ">"  # Regular transfer
                    
            else:
                # User received bytes
                other_user = transaction.giver_username
                amount_text = f"+{transaction.amount:,}"
                
                # Special handling for system rewards
                if transaction.giver_id == "SYSTEM":
                    type_indicator = "+"  # System reward
                    if (transaction.reason and 
                        transaction.reason.strip() == "New member welcome bonus"):
                        other_user = "Welcome Bonus"
                    elif (transaction.reason and 
                        transaction.reason.startswith("Daily reward")):
                        # Extract streak info from reason: "Daily reward (Day 5, 2x multiplier)"
                        if "multiplier)" in transaction.reason:
                            # Extract multiplier info
                            import re
                            match = re.search(r'Day (\d+)(?:, (\d+)x multiplier)?', transaction.reason)
                            if match:
                                day = match.group(1)
                                multiplier = match.group(2)
                                if multiplier and multiplier != "1":
                                    other_user = f"Daily ({multiplier}x)"
                                else:
                                    other_user = f"Daily (Day {day})"
                            else:
                                other_user = "Daily Reward"
                        else:
                            other_user = "Daily Reward"
                    else:
                        other_user = "System Reward"
                else:
                    type_indicator = "<"  # Regular transfer
            
            # Truncate username if too long - increased limit to show more of squad names and descriptions
            if len(other_user) > 35:
                other_user = other_user[:32] + "..."
            
            # Create compact row: Date | Type + User | Amount
            row_y = current_y + (i * 26)  # Adjusted spacing for larger font
            
            # Date (left) - no shadow for tiny text to avoid artifacts
            draw.text(
                (self.PADDING_HORIZONTAL, row_y), 
                date_str, 
                font=table_font, 
                fill=self.TEXT_COLOR
            )
            
            # Type + User (center) - no shadow for tiny text
            user_text = f"{type_indicator} {other_user}"
            user_x = self.PADDING_HORIZONTAL + 120  # Much wider spacing after date column
            draw.text(
                (user_x, row_y), 
                user_text, 
                font=table_font, 
                fill=self.TEXT_COLOR
            )
            
            # Amount (right aligned) - no shadow for tiny text
            amount_bbox = table_font.getbbox(amount_text)
            amount_width = amount_bbox[2] - amount_bbox[0]
            amount_x = self.PADDING_HORIZONTAL + content_width - amount_width
            
            # Color based on transaction type
            amount_color = "#11FF00" if transaction.giver_id != user_id else "#FF6B6B"
            
            draw.text(
                (amount_x, row_y), 
                amount_text, 
                font=table_font, 
                fill=amount_color
            )
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_config_embed(
        self, 
        config, 
        guild_name: str
    ) -> hikari.files.Bytes:
        """Create a compact configuration embed image with table layout.
        
        Args:
            config: BytesConfig object
            guild_name: Name of the guild
            
        Returns:
            hikari.File containing the generated image
        """
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = "Bytes Info"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to subtitle
        current_y += title_font.getbbox(title_text)[3] + 32
        
        # Draw subtitle with smaller font
        subtitle_font = self._fonts["text_medium"]
        subtitle_text = f"Settings for {guild_name}"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            subtitle_text, 
            subtitle_font, 
            self.TEXT_COLOR
        )
        
        # Move to content
        current_y += subtitle_font.getbbox(subtitle_text)[3] + 32
        
        # Use smaller font for table content
        table_font = self._fonts["text_small"]
        
        # Create compact table layout
        cooldown_text = "No cooldown" if config.transfer_cooldown_hours == 0 else f"{config.transfer_cooldown_hours} hours"
        config_items = [
            ("Daily Activity Reward:", f"{config.daily_amount:,} bytes"),
            ("New Member Balance:", f"{config.starting_balance:,} bytes"),
            ("Max Transfer:", f"{config.max_transfer:,} bytes"),
            ("Transfer Cooldown:", cooldown_text)
        ]
        
        # Draw main config items in two columns
        col1_width = content_width // 2
        col2_start = self.PADDING_HORIZONTAL + col1_width
        
        for i, (label, value) in enumerate(config_items):
            row_y = current_y + (i * 28)  # Tighter row spacing
            
            # Draw label (left aligned)
            self._draw_text_with_shadow(
                draw, 
                (self.PADDING_HORIZONTAL, row_y), 
                label, 
                table_font, 
                self.TEXT_COLOR
            )
            
            # Draw value (right aligned to column)
            value_bbox = table_font.getbbox(value)
            value_width = value_bbox[2] - value_bbox[0]
            value_x = col2_start + col1_width - value_width - 20  # 20px margin from right
            
            self._draw_text_with_shadow(
                draw, 
                (value_x, row_y), 
                value, 
                table_font, 
                self.TEXT_COLOR
            )
        
        # Move to streak bonuses section
        current_y += len(config_items) * 28 + 20
        
        # Streak bonuses header
        if config.streak_bonuses:
            streak_header = "Streak Bonuses:"
            self._draw_text_with_shadow(
                draw, 
                (self.PADDING_HORIZONTAL, current_y), 
                streak_header, 
                table_font, 
                self.TEXT_COLOR
            )
            
            current_y += table_font.getbbox(streak_header)[3] + 8
            
            # Draw streak bonuses in compact format
            bonus_items = []
            # Ensure proper numeric sorting by converting keys to int
            for days, multiplier in sorted(config.streak_bonuses.items(), key=lambda x: int(x[0])):
                bonus_items.append(f"{days} days: {multiplier}x")
            
            # Group bonuses on same line if they fit
            bonus_line = "  " + " • ".join(bonus_items)
            self._draw_text_with_shadow(
                draw, 
                (self.PADDING_HORIZONTAL, current_y), 
                bonus_line, 
                table_font, 
                self.TEXT_COLOR
            )
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_squad_list_embed(
        self, 
        squads: list, 
        guild_name: str, 
        current_squad_id: str = None,
        guild_roles: Optional[Dict[str, int]] = None
    ) -> hikari.files.Bytes:
        """Create a compact squad list embed image with table layout.
        
        Args:
            squads: List of squad objects
            guild_name: Name of the guild
            current_squad_id: ID of user's current squad (if any)
            
        Returns:
            hikari.File containing the generated image
        """
        if not squads:
            return self.create_simple_embed("AVAILABLE SQUADS", "No squads have been created yet!", "info")
        
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = "AVAILABLE SQUADS"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to table headers
        current_y += title_font.getbbox(title_text)[3] + 64
        
        # Use smaller font for table content
        table_font = self._fonts["text_small"]
        
        # Draw column headers
        header_font = self._fonts["text_medium"]
        
        # Header: Squad Name | Members | Join Cost
        # Squad Name header  
        name_header = "Squad Name"
        name_x = self.PADDING_HORIZONTAL
        draw.text(
            (name_x, current_y), 
            name_header, 
            font=header_font, 
            fill=self.TEXT_COLOR
        )
        
        # Members header (right-aligned in its column)
        members_header = "Members"
        members_bbox = header_font.getbbox(members_header)
        members_width = members_bbox[2] - members_bbox[0]
        # Position members column to use full width better
        members_column_start = content_width * 0.6  # 60% across the width
        members_column_width = 100
        members_x = self.PADDING_HORIZONTAL + members_column_start + members_column_width - members_width
        draw.text(
            (members_x, current_y), 
            members_header, 
            font=header_font, 
            fill=self.TEXT_COLOR
        )
        
        # Join Cost header (right-aligned)
        cost_header = "Join Cost"
        cost_bbox = header_font.getbbox(cost_header)
        cost_width = cost_bbox[2] - cost_bbox[0]
        cost_x = self.PADDING_HORIZONTAL + content_width - cost_width
        draw.text(
            (cost_x, current_y), 
            cost_header, 
            font=header_font, 
            fill=self.TEXT_COLOR
        )
        
        # Move past headers
        current_y += header_font.getbbox("A")[3] + 12
        
        # Process squads into compact table rows
        for i, squad in enumerate(squads[:10]):  # Limit to 10 squads for space
            # Create compact row: Color Circle | Name | Members | Cost
            row_y = current_y + (i * 28)  # Spacing between rows
            
            # Draw colored circle for squad role (if available)
            circle_x = self.PADDING_HORIZONTAL + 8  # 8px from left edge
            circle_y = row_y + 12  # Center vertically in row
            circle_radius = 6
            
            # Get role color or use default
            role_color = "#FFFFFF"  # Default white
            if guild_roles and hasattr(squad, 'role_id') and squad.role_id in guild_roles:
                # Convert Discord color integer to hex
                color_int = guild_roles[squad.role_id]
                if color_int != 0:  # 0 means default role color
                    role_color = f"#{color_int:06X}"
            
            self._draw_colored_circle(draw, (circle_x, circle_y), circle_radius, role_color)
            
            # Squad name (truncate if too long, adjust position for circle)
            name_text = squad.name
            if len(name_text) > 22:  # Slightly fewer characters to account for circle
                name_text = name_text[:19] + "..."
            
            name_x = self.PADDING_HORIZONTAL + 24  # Leave space for circle
            draw.text(
                (name_x, row_y), 
                name_text, 
                font=table_font, 
                fill=self.TEXT_COLOR
            )
            
            # Member count
            member_text = f"{squad.member_count}"
            if squad.max_members:
                member_text += f"/{squad.max_members}"
            
            member_bbox = table_font.getbbox(member_text)
            member_width = member_bbox[2] - member_bbox[0]
            # Right-align the member count in the column to match header
            member_x = self.PADDING_HORIZONTAL + members_column_start + members_column_width - member_width
            
            draw.text(
                (member_x, row_y), 
                member_text, 
                font=table_font, 
                fill="#00E1FF"  # Cyan for member counts
            )
            
            # Join cost or special status
            if hasattr(squad, 'is_default') and squad.is_default:
                cost_text = "Default"
                cost_color = "#f59e0b"  # Amber for default squads
            elif squad.switch_cost > 0:
                cost_text = f"{squad.switch_cost:,} bytes"
                cost_color = "#11FF00"  # Green for costs (positive feeling)
            else:
                cost_text = "Free"
                cost_color = "#11FF00"  # Green for free
            
            cost_bbox = table_font.getbbox(cost_text)
            cost_width = cost_bbox[2] - cost_bbox[0]
            cost_x = self.PADDING_HORIZONTAL + content_width - cost_width
            
            draw.text(
                (cost_x, row_y), 
                cost_text, 
                font=table_font, 
                fill=cost_color
            )
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_squad_info_embed(
        self, 
        squad, 
        members: list, 
        user_member_info=None
    ) -> hikari.files.Bytes:
        """Create a detailed squad information embed image.
        
        Args:
            squad: Squad object
            members: List of squad members
            user_member_info: User's membership information (if applicable)
            
        Returns:
            hikari.File containing the generated image
        """
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title (squad name)
        title_font = self._fonts["title_medium"]
        title_text = squad.name.upper()
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to subtitle
        current_y += title_font.getbbox(title_text)[3] + 32
        
        # Draw description if available
        if squad.description:
            desc_font = self._fonts["text_medium"]
            desc_lines = self._wrap_text_with_spacing(squad.description, desc_font, content_width)
            
            for line_text, needs_paragraph_spacing in desc_lines:
                if line_text:
                    self._draw_text_with_shadow(
                        draw, 
                        (self.PADDING_HORIZONTAL, current_y), 
                        line_text, 
                        desc_font, 
                        self.TEXT_COLOR
                    )
                
                line_height = desc_font.getbbox(line_text)[3] if line_text else desc_font.getbbox("A")[3]
                if needs_paragraph_spacing:
                    current_y += line_height + 12
                else:
                    current_y += line_height + 2
            
            current_y += 48  # Extra spacing after description
        
        # Squad stats section with full-width table
        stats_font = self._fonts["text_small"]
        
        # Skip stats header - go directly to stats content
        
        # Create full-width table for stats
        switch_cost_display = "N/A (default)" if getattr(squad, 'is_default', False) else f"{squad.switch_cost:,} bytes"
        stats_items = [
            ("Members", f"{len(members)}" + (f"/{squad.max_members}" if squad.max_members else "")),
            ("Join Cost", switch_cost_display),
            ("Status", "Active" if squad.is_active else "Inactive")
        ]
        
        # Calculate maximum label width to align values consistently
        max_label_width = 0
        for label, _ in stats_items:
            label_bbox = stats_font.getbbox(label)
            label_width = label_bbox[2] - label_bbox[0]
            max_label_width = max(max_label_width, label_width)
        
        # Position values with consistent spacing after the longest label
        value_spacing = 40  # Space between label and value
        value_x = self.PADDING_HORIZONTAL + max_label_width + value_spacing
        
        for i, (label, value) in enumerate(stats_items):
            stats_y = current_y + (i * 28)
            
            # Draw label (left aligned)
            draw.text(
                (self.PADDING_HORIZONTAL, stats_y), 
                label, 
                font=stats_font, 
                fill=self.TEXT_COLOR
            )
            
            # Draw value (aligned after longest label)
            draw.text(
                (value_x, stats_y), 
                value, 
                font=stats_font, 
                fill="#00E1FF"
            )
        
        current_y += len(stats_items) * 28 + 16
        
        # User membership info if available
        if user_member_info and user_member_info.member_since:
            member_header = "Your Membership:"
            self._draw_text_with_shadow(
                draw, 
                (self.PADDING_HORIZONTAL, current_y), 
                member_header, 
                stats_font, 
                self.TEXT_COLOR
            )
            current_y += stats_font.getbbox(member_header)[3] + 8
            
            # Membership duration
            from datetime import datetime
            membership_duration = datetime.now() - user_member_info.member_since
            days = membership_duration.days
            
            member_info = f"Member for {days} day{'s' if days != 1 else ''}"
            draw.text(
                (self.PADDING_HORIZONTAL + 20, current_y), 
                member_info, 
                font=stats_font, 
                fill="#11FF00"
            )
            current_y += stats_font.getbbox(member_info)[3] + 16
        
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_squad_members_embed(
        self, 
        squad, 
        members: list
    ) -> hikari.files.Bytes:
        """Create a squad members list embed image with table layout.
        
        Args:
            squad: Squad object
            members: List of squad members
            
        Returns:
            hikari.File containing the generated image
        """
        if not members:
            return self.create_simple_embed(squad.name.upper(), "This squad has no members.", "info")
        
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = squad.name.upper()
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to subtitle
        current_y += title_font.getbbox(title_text)[3] + 32
        
        # Draw subtitle with member count
        subtitle_font = self._fonts["text_medium"]
        subtitle_text = f"{len(members)} member{'s' if len(members) != 1 else ''}"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            subtitle_text, 
            subtitle_font, 
            self.TEXT_COLOR
        )
        
        # Move to content
        current_y += subtitle_font.getbbox(subtitle_text)[3] + 32
        
        # Use smaller font for table content
        table_font = self._fonts["text_small"]
        
        # Process members into compact table rows (show up to 15 for space)
        for i, member in enumerate(members[:15]):
            # Member name
            member_name = member.username if member.username else f"User {member.user_id[:8]}"
            if len(member_name) > 20:
                member_name = member_name[:17] + "..."
            
            # Create compact row: # | Name | Join Date
            row_y = current_y + (i * 26)
            
            # Member number
            number_text = f"{i + 1}."
            draw.text(
                (self.PADDING_HORIZONTAL, row_y), 
                number_text, 
                font=table_font, 
                fill=self.TEXT_COLOR
            )
            
            # Member name
            name_x = self.PADDING_HORIZONTAL + 40
            draw.text(
                (name_x, row_y), 
                member_name, 
                font=table_font, 
                fill=self.TEXT_COLOR
            )
            
            # Join date (if available)
            if member.joined_at:
                join_text = member.joined_at.strftime("%m/%d/%y")
                join_bbox = table_font.getbbox(join_text)
                join_width = join_bbox[2] - join_bbox[0]
                join_x = self.PADDING_HORIZONTAL + content_width - join_width
                
                draw.text(
                    (join_x, row_y), 
                    join_text, 
                    font=table_font, 
                    fill="#00E1FF"  # Cyan for dates
                )
        
        # Show truncation note if more members exist
        if len(members) > 15:
            truncate_y = current_y + (15 * 26) + 10
            truncate_text = f"... and {len(members) - 15} more members"
            draw.text(
                (self.PADDING_HORIZONTAL, truncate_y), 
                truncate_text, 
                font=table_font, 
                fill="#888888"  # Gray for truncation note
            )
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")
    
    def create_squad_join_selector_embed(
        self, 
        user_balance: int, 
        current_squad_name: str = None, 
        available_squads_count: int = 0
    ) -> hikari.files.Bytes:
        """Create a squad join selector embed image.
        
        Args:
            user_balance: User's current bytes balance
            current_squad_name: Name of user's current squad (if any)
            available_squads_count: Number of available squads
            
        Returns:
            hikari.File containing the generated image
        """
        # Load background
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["info"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = "SELECT A SQUAD TO JOIN"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to subtitle
        current_y += title_font.getbbox(title_text)[3] + 32
        
        # Draw subtitle
        subtitle_font = self._fonts["text_medium"]
        subtitle_text = "Choose a squad from the menu below."
        subtitle_lines = self._wrap_text_with_spacing(subtitle_text, subtitle_font, content_width)
        
        for line_text, needs_paragraph_spacing in subtitle_lines:
            if line_text:
                self._draw_text_with_shadow(
                    draw, 
                    (self.PADDING_HORIZONTAL, current_y), 
                    line_text, 
                    subtitle_font, 
                    self.TEXT_COLOR
                )
            
            line_height = subtitle_font.getbbox(line_text)[3] if line_text else subtitle_font.getbbox("A")[3]
            current_y += line_height + 2
        
        current_y += 20  # Extra spacing
        
        # User info section
        info_font = self._fonts["text_small"]
        
        # User balance (only show this)
        balance_text = f"Your Balance: {user_balance:,} bytes"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            balance_text, 
            info_font, 
            "#00E1FF"  # Cyan for balance
        )
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "embed.png")

    def create_balance_embed(
        self,
        username: str,
        balance: int,
        streak_count: int = 0,
        last_daily: Optional[str] = None,
        total_received: int = 0,
        total_sent: int = 0
    ) -> hikari.files.Bytes:
        """Create a compact balance embed with table layout matching other commands.
        
        Args:
            username: User's display name
            balance: Current bytes balance
            streak_count: Daily claim streak count
            last_daily: Last daily claim date string
            total_received: Total bytes received
            total_sent: Total bytes sent
            
        Returns:
            Bytes object containing the embed image
        """
        # Load background - use original size to match other commands
        background = self._get_background("info")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Title
        title_font = self._fonts["title_medium"]
        title_text = "BYTES BALANCE"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            self.COLORS["info"]
        )
        
        # Subtitle with username
        current_y += title_font.getbbox(title_text)[3] + 32
        subtitle_font = self._fonts["text_medium"]
        subtitle_text = f"Account overview for {username}"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            subtitle_text, 
            subtitle_font, 
            self.TEXT_COLOR
        )
        
        # Move to content
        current_y += subtitle_font.getbbox(subtitle_text)[3] + 32
        
        # Use smaller font for table content
        table_font = self._fonts["text_small"]
        
        # Create table rows with consistent spacing
        rows = [
            ("Balance:", f"{balance:,} bytes", "#00E1FF"),
        ]
        
        if streak_count > 0:
            rows.append(("Streak:", f"{streak_count} days", "#FF6B35"))
        
        if last_daily:
            rows.append(("Last Daily:", last_daily, "#B0B0B0"))
        
        if total_received > 0:
            rows.append(("Total Received:", f"{total_received:,}", "#11FF00"))
        
        if total_sent > 0:
            rows.append(("Total Sent:", f"{total_sent:,}", "#FF9999"))
        
        # Net calculation
        if total_received > 0 or total_sent > 0:
            net_change = total_received - total_sent
            net_color = "#11FF00" if net_change >= 0 else "#FF6B6B"
            net_prefix = "+" if net_change >= 0 else ""
            rows.append(("Net Change:", f"{net_prefix}{net_change:,}", net_color))
        
        # Draw table rows
        for label, value, color in rows:
            row_y = current_y
            
            # Left align label
            draw.text(
                (self.PADDING_HORIZONTAL, row_y),
                label,
                font=table_font,
                fill=self.TEXT_COLOR
            )
            
            # Right align value
            value_bbox = table_font.getbbox(value)
            value_width = value_bbox[2] - value_bbox[0]
            value_x = self.PADDING_HORIZONTAL + content_width - value_width
            
            draw.text(
                (value_x, row_y),
                value,
                font=table_font,
                fill=color
            )
            
            current_y += table_font.getbbox(label)[3] + 12
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "balance.png")

    def create_transfer_success_embed(
        self,
        giver_name: str,
        receiver_name: str,
        amount: int,
        reason: Optional[str] = None,
        new_balance: Optional[int] = None
    ) -> hikari.files.Bytes:
        """Create a transfer success embed image.
        
        Args:
            giver_name: Name of the user who sent bytes
            receiver_name: Name of the user who received bytes
            amount: Amount of bytes transferred
            reason: Optional reason for the transfer
            new_balance: Sender's new balance after transfer
            
        Returns:
            hikari.File containing the generated image
        """
        # Load background
        background = self._get_background("success")
        
        # Create working image - use full background size
        img = background.copy()
        draw = ImageDraw.Draw(img)
        
        # Calculate content area
        content_width = img.width - (self.PADDING_HORIZONTAL * 2)
        current_y = self.PADDING_TOP
        
        # Get colors
        title_color = self.COLORS["success"]
        
        # Draw title
        title_font = self._fonts["title_medium"]
        title_text = "BYTES TRANSFERRED SUCCESSFULLY"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to content
        current_y += title_font.getbbox(title_text)[3] + 32
        
        # Use smaller font for details
        table_font = self._fonts["text_small"]
        
        # Create table rows with transfer details
        rows = [
            ("From:", giver_name, self.TEXT_COLOR),
            ("To:", receiver_name, self.TEXT_COLOR),
            ("Amount:", f"{amount:,} bytes", "#00E1FF"),
        ]
        
        if reason:
            rows.append(("Reason:", reason, "#B0B0B0"))
        
        if new_balance is not None:
            rows.append(("New Balance:", f"{new_balance:,} bytes", "#11FF00"))
        
        # Draw table rows
        for label, value, color in rows:
            row_y = current_y
            
            # Left align label
            draw.text(
                (self.PADDING_HORIZONTAL, row_y),
                label,
                font=table_font,
                fill=self.TEXT_COLOR
            )
            
            # Right align value
            value_bbox = table_font.getbbox(value)
            value_width = value_bbox[2] - value_bbox[0]
            value_x = self.PADDING_HORIZONTAL + content_width - value_width
            
            draw.text(
                (value_x, row_y),
                value,
                font=table_font,
                fill=color
            )
            
            current_y += table_font.getbbox(label)[3] + 12
        
        # Convert to bytes
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)
        
        # Create hikari Bytes object for binary data
        return hikari.files.Bytes(img_bytes.getvalue(), "transfer_success.png")


# Global instance for easy access
_generator = None

def get_generator() -> EmbedImageGenerator:
    """Get or create the global image generator instance."""
    global _generator
    if _generator is None:
        _generator = EmbedImageGenerator()
    return _generator