"""Where a guild's incident reports go.

One channel per guild, whichever detector opened the incident: the guild's
moderation policy's ``destinations.report_channel_id`` (#241). A guild
without a policy reports nowhere; the old report-channel columns it used to
fall back on were dropped in #242.
"""

from __future__ import annotations

from smarter_dev.web.moderation_policy_store import read_policy


async def load_incident_report_channel(session, guild_id: str) -> str | None:
    """The guild's incident report channel, as its moderation policy names it.

    None while the policy's reporting is switched off, and None for a guild
    without a policy."""
    stored = await read_policy(session, guild_id)
    if stored is None:
        return None
    document = stored.document
    return document.destinations.report_channel_id if document.controls.report else None
