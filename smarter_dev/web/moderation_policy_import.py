"""Revision 1 of a guild's moderation policy (#241): every rule off.

:func:`plan` reads, and nothing else; :func:`run` writes what :func:`plan`
returned as revision 1, in one transaction. Both are run by
``scripts/moderation_policy_import.py`` as CRUD Jobs.

Only a guild the coordinator owns (``scripts/moderation_cutover.py``) and
that has no policy yet is imported. Revision 1 is
:func:`~smarter_dev.shared.moderation_policy.default_document`: every rule
and the staff-ping triage ``off``, no protected roles, no report channel, no
bump channel, and the code's values for everything else, with the acting
classifier's daily ceiling from ``MODERATION_ACTING_DAILY_REQUESTS`` (what
the bot runs with). A newly moved guild so starts with nothing switched on,
and an admin turns rules on from the Moderation page.

Until #242 the import copied revision 1 from the old per-actor config tables
(``moderation_filter_configs``' spam settings, ``attachment_filter_configs``,
``moderation_configs`` and the retired handlers' scopes). Those tables and
columns are gone; the guild that was imported from them keeps its revisions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import select

from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.models import ModerationPolicy
from smarter_dev.web.moderation_policy_store import SOURCE_IMPORT
from smarter_dev.web.moderation_policy_store import save_policy

COORDINATOR = "coordinator"
#: The acting classifier's daily ceiling, as the bot reads it.
ACTING_DAILY_REQUESTS_ENV_VAR = "MODERATION_ACTING_DAILY_REQUESTS"
DEFAULT_ACTING_DAILY_REQUESTS = 1500


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


async def plan(session, guild_id: str) -> ImportPlan:
    """What the import would write. Reads only; raises :class:`ImportRefused`."""
    if await session.get(ModerationPolicy, guild_id) is not None:
        raise ImportRefused(
            f"guild {guild_id} already has a moderation policy; nothing changed"
        )
    ownership = (
        await session.execute(
            select(ModerationOwnership).where(ModerationOwnership.guild_id == guild_id)
        )
    ).scalar_one_or_none()
    if ownership is None or ownership.owner != COORDINATOR:
        raise ImportRefused(
            f"guild {guild_id} is not moderated by the coordinator; switch it "
            "over first (scripts/moderation_cutover.py); nothing changed"
        )
    raw = mp.default_document().as_json()
    raw["classifier"]["per_guild_day"] = _acting_daily_requests()
    try:
        document = mp.validate(raw)
    except mp.PolicyError as error:
        raise ImportRefused(
            "the import's document is not a valid policy: " + "; ".join(error.errors)
        ) from None
    sources = {
        "ownership": {"owner": ownership.owner, "generation": ownership.generation},
        "document": "default_document (every rule off)",
        "classifier.per_guild_day_from": ACTING_DAILY_REQUESTS_ENV_VAR
        if os.getenv(ACTING_DAILY_REQUESTS_ENV_VAR, "").strip()
        else "default",
    }
    notes = [
        "every rule and the staff-ping triage are off, with no report channel "
        "and no protected roles: turn them on from the Moderation page"
    ]
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
