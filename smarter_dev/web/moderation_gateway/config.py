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
from smarter_dev.web.moderation_policy_store import read_policy

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


async def load_incident_report_channel(session, guild_id: str) -> str | None:
    """The guild's incident report channel, as its moderation policy names it.

    A guild with a policy (#241) has one ``destinations.report_channel_id``,
    and none while the policy's reporting is switched off; the old columns
    are not read then. Without a policy: :func:`load_report_channel`. The
    legacy actors keep reading the old columns they always read.
    """
    stored = await read_policy(session, guild_id)
    if stored is None:
        return await load_report_channel(session, guild_id)
    document = stored.document
    return document.destinations.report_channel_id if document.controls.report else None
