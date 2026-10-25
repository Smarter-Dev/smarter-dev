"""Revision 1 of a guild's moderation policy, from the config it runs on (#241).

:func:`plan` reads, and nothing else; :func:`run` writes what :func:`plan`
read as revision 1, in one transaction. Both are run by
``scripts/moderation_policy_import.py`` as CRUD Jobs.

Where each part comes from:

* **Rule modes**: the ownership row's ``rules`` (``moderation_ownership``):
  the rules the coordinator runs now are ``enforce``, the rest ``off``. Not
  the old tables' switches: the cutover (#240) turned those off, and what
  they were is in ``legacy_state``. Only a guild the coordinator owns is
  imported; a guild still on the legacy actors has nothing for a policy to
  drive.
* **Spam engine and spam ring settings, protected roles, the mod ping role,
  the scam log**: ``moderation_filter_configs`` (the ring's defaults without
  a row, as it ran).
* **Attachment tiers and notices**: ``attachment_filter_configs``.
* **Scopes**: the ``scam-banner``, ``edit-ping-catch`` and
  ``disboard-bump-tracker`` admin handlers' channels (disabled rows count:
  the cutover disabled them; their scope is what the coordinator ported).
* **The report channel**: the order the gateway already resolves it in
  (``moderation_gateway.config``).
* **Staff-ping triage**: ``moderation_configs``. ``enabled_tools`` and
  ``response_channel_id`` are copied as they are: an empty list stays empty,
  and no reply channel still means the ping's own channel.
* **A missing source row** turns its rules ``off``, with a note: no
  ``moderation_filter_configs`` row, no spam engine or spam ring; no
  ``attachment_filter_configs`` row, no attachment filter. Defaults never
  switch a rule on.
* **Everything else** (risk windows, the scam ladder, the provisional hold
  and its signals, the classifier's budgets): the values the code runs with,
  with the acting daily ceiling from ``MODERATION_ACTING_DAILY_REQUESTS``.

The old tables are not changed: they stay for a rollback until #242.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import select

from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import AttachmentFilterConfig
from smarter_dev.web.models import ModerationConfig
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.models import ModerationPolicy
from smarter_dev.web.moderation_gateway.config import first_channel
from smarter_dev.web.moderation_policy_store import SOURCE_IMPORT
from smarter_dev.web.moderation_policy_store import save_policy

COORDINATOR = "coordinator"
SCAM_BANNER_HANDLER = "scam-banner"
EDIT_PING_HANDLER = "edit-ping-catch"
BUMP_HANDLER = "disboard-bump-tracker"
#: The acting classifier's daily ceiling, as the bot reads it.
ACTING_DAILY_REQUESTS_ENV_VAR = "MODERATION_ACTING_DAILY_REQUESTS"
DEFAULT_ACTING_DAILY_REQUESTS = 1500

_SPAM_FIELDS = (
    "message_rate_threshold",
    "message_rate_window_seconds",
    "channel_spread_threshold",
    "channel_spread_window_seconds",
    "duplicate_message_min_length",
    "duplicate_message_window_seconds",
    "mass_mention_window_seconds",
    "warning_reoffense_window_seconds",
    "mute_duration_seconds",
)


class ImportRefused(Exception):
    """The import was refused; nothing was changed."""


@dataclass(slots=True)
class ImportPlan:
    guild_id: str
    document: mp.PolicyDocument
    #: Where each part came from, for the dry run.
    sources: dict[str, Any] = field(default_factory=dict)
    #: What a reader should check before the import is run.
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "document": self.document.as_json(),
            "sources": self.sources,
            "notes": self.notes,
        }


def _acting_daily_requests() -> int:
    raw = os.getenv(ACTING_DAILY_REQUESTS_ENV_VAR, "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_ACTING_DAILY_REQUESTS
    except ValueError:
        return DEFAULT_ACTING_DAILY_REQUESTS


async def _one(session, model, guild_id: str):
    return (
        await session.execute(select(model).where(model.guild_id == guild_id))
    ).scalar_one_or_none()


async def plan(session, guild_id: str) -> ImportPlan:
    """What the import would write. Reads only; raises :class:`ImportRefused`."""
    if await session.get(ModerationPolicy, guild_id) is not None:
        raise ImportRefused(
            f"guild {guild_id} already has a moderation policy; nothing changed"
        )
    ownership = await _one(session, ModerationOwnership, guild_id)
    if ownership is None or ownership.owner != COORDINATOR:
        raise ImportRefused(
            f"guild {guild_id} is not moderated by the coordinator; switch it "
            "over first (scripts/moderation_cutover.py); nothing changed"
        )
    running = {str(rule) for rule in ownership.rules or ()}
    unknown = sorted(running - set(mp.RULES))
    filters = await _one(session, ModerationFilterConfig, guild_id)
    attachments = await _one(session, AttachmentFilterConfig, guild_id)
    triage = await _one(session, ModerationConfig, guild_id)
    handlers = {
        row.name: row
        for row in (
            await session.execute(
                select(AdminHandler).where(
                    AdminHandler.guild_id == guild_id,
                    AdminHandler.name.in_(
                        (SCAM_BANNER_HANDLER, EDIT_PING_HANDLER, BUMP_HANDLER)
                    ),
                )
            )
        ).scalars()
    }
    notes: list[str] = []
    if unknown:
        notes.append(f"ownership rules not in the policy, left out: {unknown}")
    config = filters or ModerationFilterConfig.get_defaults(guild_id)
    missing: set[str] = set()
    if filters is None:
        missing |= {mp.SPAM_ENGINE, mp.SPAM_RING}
    if attachments is None:
        missing.add(mp.ATTACHMENT_FILTER)
    for rule in sorted(missing & running):
        notes.append(
            f"{rule} runs but its config row is missing: imported as off "
            "(settings at their defaults)"
        )

    def mode(rule: str) -> str:
        return mp.ENFORCE if rule in running and rule not in missing else mp.OFF

    def scope(name: str) -> list[str]:
        row = handlers.get(name)
        return [str(channel) for channel in (row.channel_ids or [])] if row else []

    bump = handlers.get(BUMP_HANDLER)
    bump_channels = scope(BUMP_HANDLER) if bump is not None and bump.enabled else []
    if mp.BUMP_CHANNEL in running and not bump_channels:
        notes.append(
            "bump_channel runs but the Disboard handler names no channel (or is "
            "disabled): the rule has no channel, as now"
        )
    report_channel = first_channel(
        config.spam_ring_report_channel_id,
        config.mod_alert_channel_id,
        config.mod_log_channel_id,
        triage.response_channel_id if triage is not None else None,
    )
    if report_channel is None:
        notes.append("no report channel is set anywhere: incidents report nowhere")
    triage_tools = list(triage.enabled_tools or []) if triage is not None else []
    if triage is not None and not isinstance(triage.enabled_tools, list):
        triage_tools = []
    known_tools = [tool for tool in triage_tools if tool in mp.TRIAGE_TOOLS]
    if len(known_tools) != len(triage_tools):
        notes.append(f"unknown triage tools dropped: {triage_tools}")
    if triage is None:
        notes.append("no moderation_configs row: staff-ping triage off")

    raw = {
        "schema": mp.SCHEMA_VERSION,
        "controls": {"evaluate": True, "enforce": True, "report": True},
        "protected_role_ids": [str(r) for r in config.staff_exempt_role_ids or []],
        "rules": {
            mp.SPAM_ENGINE: {
                "mode": mode(mp.SPAM_ENGINE),
                **{name: getattr(config, name) for name in _SPAM_FIELDS},
                "scam_link_domains": list(config.scam_link_domains or []),
                "scam_log_channel_id": config.scam_log_channel_id,
            },
            mp.ATTACHMENT_FILTER: {
                "mode": mode(mp.ATTACHMENT_FILTER),
                "ignored_extensions": list(attachments.ignored_extensions or [])
                if attachments
                else [],
                "warn_extensions": list(attachments.warn_extensions or [])
                if attachments
                else [],
                "warn_message": attachments.warn_message if attachments else None,
                "delete_message": attachments.delete_message if attachments else None,
            },
            mp.SPAM_RING: {
                "mode": mode(mp.SPAM_RING),
                "new_member_days": config.spam_ring_new_member_days,
                "timeout_seconds": config.spam_ring_timeout_seconds,
                "lookback_minutes": config.spam_ring_lookback_minutes,
                "followup_minutes": config.spam_ring_followup_minutes,
            },
            mp.SCAM_GATE: {
                "mode": mode(mp.SCAM_GATE),
                "channels": scope(SCAM_BANNER_HANDLER),
            },
            mp.EDIT_PING: {
                "mode": mode(mp.EDIT_PING),
                "channels": scope(EDIT_PING_HANDLER),
            },
            mp.BUMP_CHANNEL: {
                "mode": mode(mp.BUMP_CHANNEL),
                "channels": bump_channels,
            },
            mp.MASS_MENTION: {"mode": mode(mp.MASS_MENTION)},
        },
        "classifier": {"per_guild_day": _acting_daily_requests()},
        "destinations": {
            "report_channel_id": report_channel,
            "mod_ping_role_id": config.mod_ping_role_id,
        },
        "triage": {
            "mode": mp.ENFORCE if triage is not None and triage.is_active else mp.OFF,
            "monitored_role_ids": [str(r) for r in triage.monitored_role_ids or []]
            if triage
            else [],
            "instructions": (triage.instructions or "") if triage else "",
            "enabled_tools": known_tools,
            "context_message_limit": triage.context_message_limit
            if triage and triage.context_message_limit
            else 25,
            "response_channel_id": triage.response_channel_id if triage else None,
        },
    }
    try:
        document = mp.validate(raw)
    except mp.PolicyError as error:
        raise ImportRefused(
            "the current config is not a valid policy: " + "; ".join(error.errors)
        ) from None
    sources = {
        "ownership": {
            "owner": ownership.owner,
            "generation": ownership.generation,
            "rules": sorted(running),
        },
        "moderation_filter_configs": filters is not None,
        "attachment_filter_configs": attachments is not None,
        "moderation_configs": triage is not None,
        "handlers": {
            name: {"enabled": row.enabled, "channel_ids": list(row.channel_ids or [])}
            for name, row in sorted(handlers.items())
        },
        "report_channel_from": [
            "spam_ring_report_channel_id",
            "mod_alert_channel_id",
            "mod_log_channel_id",
            "moderation_configs.response_channel_id",
        ],
        "classifier.per_guild_day_from": ACTING_DAILY_REQUESTS_ENV_VAR
        if os.getenv(ACTING_DAILY_REQUESTS_ENV_VAR, "").strip()
        else "default",
    }
    return ImportPlan(
        guild_id=guild_id, document=document, sources=sources, notes=notes
    )


async def run(session, guild_id: str, *, by: str) -> ImportPlan:
    """Write the plan as revision 1. Commits, or raises :class:`ImportRefused`."""
    result = await plan(session, guild_id)
    await save_policy(
        session,
        guild_id,
        result.document,
        base_revision=None,
        by=by,
        source=SOURCE_IMPORT,
    )
    return result
