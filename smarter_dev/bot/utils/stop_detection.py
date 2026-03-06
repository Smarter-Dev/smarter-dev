"""Stop detection utility for Discord bot.

Regex-based stop detection — no LLM call, instant response.
Only triggers on @mentions to avoid false positives.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta

# Pattern to strip Discord @mention markup: <@123456> or <@!123456>
MENTION_PATTERN = re.compile(r"<@!?\d+>")

# Stop phrases — checked against stripped, lowercased content.
# Order: longer phrases first to avoid partial matches.
STOP_PHRASES = [
    "leave me alone",
    "knock it off",
    "that's enough",
    "thats enough",
    "i'm done",
    "im done",
    "be quiet",
    "no more",
    "shut up",
    "go away",
    "enough",
    "stfu",
    "hush",
    "stop",
]

# Compiled pattern: match any stop phrase as the entire message content
# (after stripping mentions and whitespace)
STOP_PATTERN = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in STOP_PHRASES) + r")[.!?]*$",
    re.IGNORECASE,
)

# False positive patterns — messages that contain stop words but aren't stop requests
FALSE_POSITIVE_PATTERNS = [
    re.compile(r"can'?t stop", re.IGNORECASE),
    re.compile(r"won'?t stop", re.IGNORECASE),
    re.compile(r"don'?t stop", re.IGNORECASE),
    re.compile(r"never stop", re.IGNORECASE),
    re.compile(r"stop light", re.IGNORECASE),
    re.compile(r"stop sign", re.IGNORECASE),
    re.compile(r"stop by", re.IGNORECASE),
    re.compile(r"stop and", re.IGNORECASE),
    re.compile(r"stop for", re.IGNORECASE),
    re.compile(r"stop at", re.IGNORECASE),
    re.compile(r"full stop", re.IGNORECASE),
    re.compile(r"bus stop", re.IGNORECASE),
    re.compile(r"pit stop", re.IGNORECASE),
]

# Channel cooldown state: channel_id → cooldown expiry time
_channel_cooldowns: dict[int, datetime] = {}

# Acknowledgment messages when bot was actively watching
STOP_ACKS_ACTIVE = [
    "alright, I'll stop. I'll stay quiet here for 5 minutes",
    "got it, backing off for 5 minutes",
    "okay, going quiet for 5 minutes",
    "understood, I'll leave it alone. Back in 5 if you need me",
    "fair enough, I'm out for 5 minutes",
    "no problem, shutting up for 5 minutes",
    "roger that, going silent for 5 minutes",
]

# Acknowledgment messages when bot wasn't doing anything
STOP_ACKS_IDLE = [
    "I wasn't doing anything, but noted!",
    "okay! I wasn't watching this channel anyway",
    "sure thing — I wasn't active here, but I hear you",
]


def is_stop_request(content: str) -> bool:
    """Check if a message is a stop/dismissal request.

    Strips @mention markup before checking against stop patterns.
    Designed to be called on @mention messages only.

    Args:
        content: Raw message content (may include <@123> mention markup)

    Returns:
        True if the message is a stop request
    """
    # Strip mention markup and whitespace
    stripped = MENTION_PATTERN.sub("", content).strip()

    if not stripped:
        return False

    # Check false positives first (on original stripped text)
    for fp in FALSE_POSITIVE_PATTERNS:
        if fp.search(stripped):
            return False

    # Check if the stripped message matches a stop phrase
    return bool(STOP_PATTERN.match(stripped))


def random_stop_ack(had_watchers: bool) -> str:
    """Return a casual acknowledgment for a stop request.

    Args:
        had_watchers: True if the bot had active watchers in the channel

    Returns:
        A random acknowledgment message
    """
    if had_watchers:
        return random.choice(STOP_ACKS_ACTIVE)
    return random.choice(STOP_ACKS_IDLE)


def set_channel_cooldown(channel_id: int, duration_seconds: int = 300) -> None:
    """Set a cooldown on a channel, preventing the bot from responding.

    Args:
        channel_id: Discord channel ID
        duration_seconds: Cooldown duration in seconds (default 5 minutes)
    """
    _channel_cooldowns[channel_id] = datetime.now(UTC) + timedelta(seconds=duration_seconds)


def is_channel_on_cooldown(channel_id: int) -> bool:
    """Check if a channel is on cooldown.

    Auto-cleans expired entries.

    Args:
        channel_id: Discord channel ID

    Returns:
        True if the channel is on cooldown
    """
    expires_at = _channel_cooldowns.get(channel_id)
    if expires_at is None:
        return False
    if datetime.now(UTC) >= expires_at:
        del _channel_cooldowns[channel_id]
        return False
    return True
