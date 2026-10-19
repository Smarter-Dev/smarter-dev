"""Where a guild's incident reports go.

One channel per guild, whichever detector opened the incident. The order is
the one the spam ring already used: its own report channel, then the spam
engine's alert channel, then the content filter's mod log, then the staff-ping
triage's response channel. On smarter.dev every one of those that is set is
728249959098482829.
"""

from __future__ import annotations

from smarter_dev.web.crud import ModerationConfigOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.models import ModerationFilterConfig

_filter_configs = ModerationFilterConfigOperations()
_triage_configs = ModerationConfigOperations()


def first_channel(*candidates: str | None) -> str | None:
    """The first configured channel, in the order given."""
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def report_channel_from(
    config: ModerationFilterConfig | None, triage_response_channel_id: str | None = None
) -> str | None:
    if config is None:
        return first_channel(triage_response_channel_id)
    return first_channel(
        config.spam_ring_report_channel_id,
        config.mod_alert_channel_id,
        config.mod_log_channel_id,
        triage_response_channel_id,
    )


async def load_report_channel(
    session, guild_id: str, config: ModerationFilterConfig | None = None
) -> str | None:
    """The guild's incident report channel; reads only what it needs."""
    if config is None:
        config = await _filter_configs.get_config(session, guild_id)
    channel = report_channel_from(config)
    if channel is not None:
        return channel
    triage = await _triage_configs.get_config(session, guild_id)
    return first_channel(triage.response_channel_id if triage is not None else None)
