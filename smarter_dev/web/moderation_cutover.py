"""The moderation cutover (#240): one guild from the legacy actors to the
coordinator, in one transaction.

``switch`` does all of this or none of it:

* writes the guild's ``moderation_ownership`` row: owner ``coordinator``, the
  next generation, the rules the coordinator now runs (those of the legacy
  actors that were on), and ``legacy_state``, exactly what is changed below;
* turns the legacy actors off by their own switches: the spam engine and spam
  ring (``moderation_filter_configs``), the attachment filter
  (``attachment_filter_configs.is_active``), the ``scam-banner`` and
  ``edit-ping-catch`` admin handlers (rows disabled, not deleted, and the
  edit-ping-catch install with them);
* moves Disboard's channel restriction: the live bump tracker's "delete
  anything not from Disboard" becomes "delete other bots' messages", leaving
  members' chatter to the coordinator's bump-channel rule; scoring, the crown
  and the reminders stay. Only that row is patched (the catalog is not), and
  the switch refuses if the restriction is not found exactly once.

The bot reads the ownership row with every message, so the coordinator acts on
the next message after the commit. The bot-core legacy actors (spam engine,
spam ring, attachment filter) and the scam-banner script and edit-ping-catch
template were removed after the cutover, so the switches above are now only
the record of which rules the guild had on; a disabled handler's pending
timers are skipped when they fire.

There is no rollback any more: the legacy actors it would restore no longer
exist. ``legacy_state`` is still saved on the ownership row, as a record of
what the switch changed.

``status`` only reads; once the retired rows are deleted
(``scripts/delete_retired_moderation_rows.py``) its handler and install
entries for them are simply absent.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import ownership as own
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import AttachmentFilterConfig
from smarter_dev.web.models import ExtensionInstall
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationOwnership

SCAM_BANNER_HANDLER = "scam-banner"
EDIT_PING_HANDLER = "edit-ping-catch"
BUMP_HANDLER = "disboard-bump-tracker"
EDIT_PING_SLUG = "edit-ping-catch"
DISBOARD_SLUG = "disboard-bumping"
#: The handlers the coordinator replaces outright.
RETIRED_HANDLERS = (SCAM_BANNER_HANDLER, EDIT_PING_HANDLER)


class CutoverRefused(Exception):
    """The switch was refused; nothing was changed."""


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


async def _row(session, model, **where):
    query = select(model)
    for name, value in where.items():
        query = query.where(getattr(model, name) == value)
    return (await session.execute(query.with_for_update())).scalar_one_or_none()


async def _handlers(session, guild_id: str) -> dict[str, AdminHandler]:
    rows = (
        await session.execute(
            select(AdminHandler)
            .where(
                AdminHandler.guild_id == guild_id,
                AdminHandler.name.in_((*RETIRED_HANDLERS, BUMP_HANDLER)),
            )
            .with_for_update()
        )
    ).scalars()
    return {row.name: row for row in rows}


#: The live bump tracker's restriction, exactly as the catalog renders it
#: (every version so far), and what the switch puts in its place: members'
#: chatter is the coordinator's ``bump_channel`` rule; other bots' messages
#: are still cleared by the handler. Only the live row is patched, never the
#: catalog, so a guild still on the legacy actors keeps its cleanup.
DISBOARD_RESTRICTION = (
    '    if context["author_id"] != DISBOARD_ID:\n'
    '        await delete_message(context["message_id"], BUMP_CHANNEL)\n'
    "        return\n"
)
DISBOARD_BOTS_ONLY = (
    '    if context["author_id"] != DISBOARD_ID:\n'
    "        # Members' chatter is the moderation coordinator's (#240).\n"
    '        if context.get("author_is_bot"):\n'
    '            await delete_message(context["message_id"], BUMP_CHANNEL)\n'
    "        return\n"
)


def _disboard_script(row: AdminHandler) -> str:
    """The live bump tracker with its member restriction handed over."""
    script = row.script or ""
    if script.count(DISBOARD_RESTRICTION) != 1:
        raise CutoverRefused(
            "the live Disboard bump tracker is not the catalog's (its channel "
            "restriction was not found exactly once); nothing changed"
        )
    return script.replace(DISBOARD_RESTRICTION, DISBOARD_BOTS_ONLY)


async def status(session, guild_id: str) -> dict[str, Any]:
    """Everything the switch reads and writes, as it is now. Reads only."""
    ownership = (
        await session.execute(
            select(ModerationOwnership).where(ModerationOwnership.guild_id == guild_id)
        )
    ).scalar_one_or_none()
    config = (
        await session.execute(
            select(ModerationFilterConfig).where(
                ModerationFilterConfig.guild_id == guild_id
            )
        )
    ).scalar_one_or_none()
    attachments = (
        await session.execute(
            select(AttachmentFilterConfig).where(
                AttachmentFilterConfig.guild_id == guild_id
            )
        )
    ).scalar_one_or_none()
    handlers = (
        await session.execute(
            select(AdminHandler).where(
                AdminHandler.guild_id == guild_id,
                AdminHandler.name.in_((*RETIRED_HANDLERS, BUMP_HANDLER)),
            )
        )
    ).scalars()
    installs = (
        await session.execute(
            select(ExtensionInstall).where(
                ExtensionInstall.guild_id == guild_id,
                ExtensionInstall.extension_slug.in_((EDIT_PING_SLUG, DISBOARD_SLUG)),
            )
        )
    ).scalars()
    return {
        "guild_id": guild_id,
        "owner": ownership.owner if ownership else own.LEGACY,
        "generation": ownership.generation if ownership else 0,
        "rules": list(ownership.rules or []) if ownership else [],
        "rollback_saved": bool(ownership and ownership.legacy_state),
        "changed_by": ownership.changed_by if ownership else None,
        "changed_at": ownership.changed_at.isoformat()
        if ownership and ownership.changed_at
        else None,
        "spam_engine_enabled": config.spam_engine_enabled if config else None,
        "spam_ring_enabled": config.spam_ring_enabled if config else None,
        "attachment_filter_active": attachments.is_active if attachments else None,
        "handlers": {
            row.name: {"enabled": row.enabled, "script_sha256": _sha(row.script)}
            for row in handlers
        },
        "extension_installs": {
            row.extension_slug: {
                "enabled": row.enabled,
                "installed_version": row.installed_version,
            }
            for row in installs
        },
    }


async def switch(session, guild_id: str, *, by: str) -> dict[str, Any]:
    """Hand the guild to the coordinator. Commits, or raises CutoverRefused."""
    ownership = await _row(session, ModerationOwnership, guild_id=guild_id)
    if ownership is not None and ownership.owner == own.COORDINATOR:
        raise CutoverRefused(
            f"guild {guild_id} is already owned by the coordinator "
            f"(generation {ownership.generation}); nothing changed"
        )
    if ownership is not None and ownership.legacy_state:
        raise CutoverRefused(
            "saved legacy state is still on the ownership row; nothing changed"
        )
    config = await _row(session, ModerationFilterConfig, guild_id=guild_id)
    if config is None:
        # Without a row the spam ring runs on its defaults and cannot be
        # switched off.
        raise CutoverRefused(
            f"guild {guild_id} has no moderation_filter_configs row; nothing changed"
        )
    attachments = await _row(session, AttachmentFilterConfig, guild_id=guild_id)
    handlers = await _handlers(session, guild_id)
    edit_install = await _row(
        session, ExtensionInstall, guild_id=guild_id, extension_slug=EDIT_PING_SLUG
    )

    scam = handlers.get(SCAM_BANNER_HANDLER)
    edit = handlers.get(EDIT_PING_HANDLER)
    bump = handlers.get(BUMP_HANDLER)
    rules: list[str] = []
    if config.spam_engine_enabled:
        rules.append(own.SPAM_ENGINE)
    if attachments is not None and attachments.is_active:
        rules.append(own.ATTACHMENT_FILTER)
    if config.spam_ring_enabled:
        rules.append(own.SPAM_RING)
    if scam is not None and scam.enabled:
        rules.append(own.SCAM_GATE)
    if edit is not None and edit.enabled:
        rules.append(own.EDIT_PING)
    if bump is not None and bump.enabled and bump.channel_ids:
        rules.append(own.BUMP_CHANNEL)
    if not rules:
        raise CutoverRefused(
            f"no legacy moderation is on in guild {guild_id}; nothing to hand over"
        )
    # Not a hand-over: a rule only the coordinator has, on with it.
    rules.append(own.MASS_MENTION)

    disboard: dict[str, Any] | None = None
    if own.BUMP_CHANNEL in rules:
        script = _disboard_script(bump)
        disboard = {"script": bump.script, "new_script_sha256": _sha(script)}

    legacy_state = {
        "spam_engine_enabled": bool(config.spam_engine_enabled),
        "spam_ring_enabled": bool(config.spam_ring_enabled),
        "attachment_filter_active": None
        if attachments is None
        else bool(attachments.is_active),
        "handlers": {
            name: bool(row.enabled)
            for name, row in handlers.items()
            if name in RETIRED_HANDLERS
        },
        "edit_ping_install_enabled": None
        if edit_install is None
        else bool(edit_install.enabled),
        "disboard": disboard,
    }

    config.spam_engine_enabled = False
    config.spam_ring_enabled = False
    if attachments is not None:
        attachments.is_active = False
    for name in RETIRED_HANDLERS:
        if name in handlers:
            handlers[name].enabled = False
    if edit_install is not None:
        edit_install.enabled = False
    if disboard is not None:
        bump.script = script

    now = datetime.now(UTC)
    if ownership is None:
        ownership = ModerationOwnership(guild_id=guild_id, generation=0)
        session.add(ownership)
    ownership.owner = own.COORDINATOR
    ownership.generation = int(ownership.generation or 0) + 1
    ownership.rules = rules
    ownership.legacy_state = legacy_state
    ownership.changed_by = by[:100]
    ownership.changed_at = now
    await session.commit()
    return await status(session, guild_id)
