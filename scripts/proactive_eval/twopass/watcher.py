"""Shim: the two-pass core moved to smarter_dev.bot.proactive.watcher."""

from smarter_dev.bot.proactive.watcher import (  # noqa: F401
    SKIM_SYSTEM_PROMPT,
    SkimRunner,
    WATCHER_SYSTEM_PROMPT,
    WatcherDecision,
    WatcherRunner,
    build_watcher_prompt,
    usage_dict,
)
