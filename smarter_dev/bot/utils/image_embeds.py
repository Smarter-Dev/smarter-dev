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
            # Bruno Ace SC for both titles and text - thicker, more readable on mobile
            bruno_path = self.fonts_path / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf"
            if bruno_path.exists():
                # Title fonts - using Bruno for better mobile readability
                self._fonts["title_large"] = ImageFont.truetype(str(bruno_path), 60)
                self._fonts["title_medium"] = ImageFont.truetype(str(bruno_path), 48)
                self._fonts["title_small"] = ImageFont.truetype(str(bruno_path), 36)
                
                # Body text fonts - slightly smaller Bruno
                self._fonts["text_large"] = ImageFont.truetype(str(bruno_path), 32)
                self._fonts["text_medium"] = ImageFont.truetype(str(bruno_path), 28)
                self._fonts["text_small"] = ImageFont.truetype(str(bruno_path), 24)
                self._fonts["text_tiny"] = ImageFont.truetype(str(bruno_path), 20)
            
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
        current_y += 24
        
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
        """Create a leaderboard embed image.
        
        Args:
            entries: List of leaderboard entries
            guild_name: Name of the guild
            user_display_names: Mapping of user IDs to display names
            
        Returns:
            hikari.File containing the generated image
        """
        if not entries:
            return self.create_simple_embed("BYTES LEADERBOARD", "No leaderboard data available yet!", "info")
        
        # Build leaderboard text
        lines = [f"Top {len(entries)} users in {guild_name}:", ""]
        
        for entry in entries:
            # Remove emojis that don't render with custom fonts
            rank_text = {1: "#1", 2: "#2", 3: "#3"}.get(entry.rank, f"#{entry.rank}")
            display_name = user_display_names.get(entry.user_id, f"User {entry.user_id[:8]}")
            
            line = f"{rank_text} {display_name}"
            line += f"\n    {entry.balance:,} bytes"
            
            if entry.streak_count > 0:
                line += f" - {entry.streak_count} day streak"
            
            lines.append(line)
        
        description = "\n".join(lines)
        return self.create_simple_embed("BYTES LEADERBOARD", description, "info")
    
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
        
        # Move to subtitle
        current_y += title_font.getbbox(title_text)[3] + 16
        
        # Draw subtitle with smaller font
        subtitle_font = self._fonts["text_medium"]
        subtitle_text = f"Your last {len(transactions)} transactions"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            subtitle_text, 
            subtitle_font, 
            self.TEXT_COLOR
        )
        
        # Move to content
        current_y += subtitle_font.getbbox(subtitle_text)[3] + 20
        
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
                type_indicator = ">"  # Simple ASCII arrow
                other_user = transaction.receiver_username
                amount_text = f"-{transaction.amount:,}"
            else:
                # User received bytes
                type_indicator = "<"  # Simple ASCII arrow
                other_user = transaction.giver_username
                amount_text = f"+{transaction.amount:,}"
            
            # Truncate username if too long
            if len(other_user) > 15:
                other_user = other_user[:12] + "..."
            
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
        title_text = "BYTES ECONOMY CONFIG"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            title_text, 
            title_font, 
            title_color
        )
        
        # Move to subtitle
        current_y += title_font.getbbox(title_text)[3] + 16
        
        # Draw subtitle with smaller font
        subtitle_font = self._fonts["text_medium"]
        subtitle_text = f"Configuration for {guild_name}"
        self._draw_text_with_shadow(
            draw, 
            (self.PADDING_HORIZONTAL, current_y), 
            subtitle_text, 
            subtitle_font, 
            self.TEXT_COLOR
        )
        
        # Move to content
        current_y += subtitle_font.getbbox(subtitle_text)[3] + 24
        
        # Use smaller font for table content
        table_font = self._fonts["text_small"]
        
        # Create compact table layout
        config_items = [
            ("Daily Amount:", f"{config.daily_amount:,} bytes"),
            ("Starting Balance:", f"{config.starting_balance:,} bytes"),
            ("Max Transfer:", f"{config.max_transfer:,} bytes"),
            ("Transfer Cooldown:", f"{config.transfer_cooldown_hours} hours")
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


# Global instance for easy access
_generator = None

def get_generator() -> EmbedImageGenerator:
    """Get or create the global image generator instance."""
    global _generator
    if _generator is None:
        _generator = EmbedImageGenerator()
    return _generator