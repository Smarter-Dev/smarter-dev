"""Shim: the two-pass core moved to smarter_dev.bot.proactive.windows."""

from smarter_dev.bot.proactive.windows import (  # noqa: F401
    MAX_WAIT_SECONDS,
    PASSIVE_SECONDS,
    QUIET_SECONDS,
    burst_windows,
    two_pass_windows,
)
