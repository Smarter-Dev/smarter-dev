"""Shim: the two-pass core moved to smarter_dev.bot.proactive.adapter."""

from smarter_dev.bot.proactive.adapter import (  # noqa: F401
    WATCHER_CONTEXT_SIZE,
    TwoPassAdapter,
    bot_directed_message_ids,
    build_wake_brief,
    engagement_notifications,
    render_active_instructions,
)
