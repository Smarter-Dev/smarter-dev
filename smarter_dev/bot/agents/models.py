"""Data models for Discord bot agents."""

from datetime import datetime

from pydantic import BaseModel


class DiscordMessage(BaseModel):
    """Represents a Discord message for context."""
    author: str
    author_id: str | None = None  # Discord user ID as string
    timestamp: datetime
    content: str
    # Reply context - populated when this message is a reply to another message
    replied_to_author: str | None = None  # Author of the message being replied to
    replied_to_content: str | None = None  # Content of the message being replied to
    # Channel context information
    channel_name: str | None = None  # Name of the channel
    channel_description: str | None = None  # Channel description/topic
    channel_type: str | None = None  # Channel type (text, forum, etc.)
    # User role information
    author_roles: list[str] = []  # List of role names for the message author
    # Forum post context
    is_original_poster: bool = False  # True if this is the OP in a forum thread
