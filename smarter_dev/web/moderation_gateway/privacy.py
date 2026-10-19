"""A data-deletion request against the moderation gateway's records.

The privacy notice keeps moderation history after a deletion request
("Moderation history, to keep the server safe"), so incidents, their links,
reason and note entries, operations and ``moderation_actions`` stay. What a
request removes is what the person posted and what a model or a pending
delivery says about them:

* evidence entries quoting their messages;
* transcripts and model assessments that name them (by id);
* outbox rows about them (a notice or DM not yet settled).

``docs/privacy-deletion-runbook.md`` step 6 runs the same deletes as SQL; this
function is the tested statement of what they must do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationOutbox
from smarter_dev.web.moderation_gateway import types as t

#: Entry kinds that can hold what the person wrote or a model wrote about them.
CONTENT_KINDS = (t.ENTRY_EVIDENCE, t.ENTRY_TRANSCRIPT, t.ENTRY_ASSESSMENT)


@dataclass(frozen=True, slots=True)
class MemberContentDeletion:
    entries: int
    outbox: int


def _names(text: str | None, discord_id: str) -> bool:
    """The id as a whole number in ``text``, as the runbook's regex matches it."""
    return (
        bool(text)
        and re.search(rf"(^|[^0-9]){re.escape(discord_id)}([^0-9]|$)", text) is not None
    )


async def delete_member_moderation_content(
    session: AsyncSession, discord_id: str
) -> MemberContentDeletion:
    """Delete the person's content from the gateway's records; the caller commits."""
    did = str(discord_id)
    candidates = (
        await session.execute(
            select(
                ModerationIncidentEntry.id,
                ModerationIncidentEntry.target_user_id,
                ModerationIncidentEntry.content,
            ).where(
                ModerationIncidentEntry.kind.in_(CONTENT_KINDS),
                or_(
                    ModerationIncidentEntry.target_user_id == did,
                    ModerationIncidentEntry.content.contains(did),
                ),
            )
        )
    ).all()
    doomed = [
        entry_id
        for entry_id, author_id, content in candidates
        if author_id == did or _names(content, did)
    ]
    if doomed:
        await session.execute(
            delete(ModerationIncidentEntry).where(
                ModerationIncidentEntry.id.in_(doomed)
            )
        )
    outbox = await session.execute(
        delete(ModerationOutbox).where(ModerationOutbox.target_user_id == did)
    )
    return MemberContentDeletion(entries=len(doomed), outbox=outbox.rowcount or 0)
