"""The Disboard bump tracker's channels come from the moderation policy (#242).

The bump channel used to be stored twice: as the bump-tracker admin handler's
own ``channel_ids`` (its dispatch scope for scoring) and as the policy's
``rules.bump_channel.channels`` (the coordinator's "members' chatter is
removed here" rule, imported from the handler at revision 1). Edited in two
places, the two could drift apart.

The policy's list is now the only one. The tracker fires in those channels
whatever the rule's mode is: the mode decides whether members' chatter is
removed, not whether bumps are scored. A guild with no policy, or a policy
naming no bump channel, gives the tracker no channel, so it does nothing.
That differs from every other handler, where an empty scope means the whole
guild. The handler row's own ``channel_ids`` are not read, and the handler
API refuses to change them.
"""

from __future__ import annotations

import logging

from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.moderation_policy import PolicyError
from smarter_dev.web.moderation_policy_store import read_policy

logger = logging.getLogger(__name__)

#: The catalog's bump-tracker template key, and the name its row is given.
BUMP_TRACKER_KEY = "bump-tracker"
BUMP_TRACKER_HANDLER = "disboard-bump-tracker"

CHANNELS_REFUSAL = (
    "the Disboard bump tracker runs in the bump channels set on the server's "
    "moderation page (Bump channel), so its channels can't be changed here"
)


def is_bump_tracker(handler) -> bool:
    """Whether an admin handler row is the Disboard bump tracker."""
    return (
        getattr(handler, "extension_handler_key", None) == BUMP_TRACKER_KEY
        or getattr(handler, "name", None) == BUMP_TRACKER_HANDLER
    )


async def bump_channel_ids(session, guild_id: str) -> list[str]:
    """The policy's bump channels; ``[]`` without a policy.

    A policy this code cannot read also gives ``[]`` (logged): the tracker
    stops rather than guessing, and other handlers are not affected."""
    try:
        stored = await read_policy(session, str(guild_id))
    except PolicyError:
        log_exception(
            logger,
            f"Unreadable moderation policy for guild {guild_id}; bump tracker off",
        )
        return []
    if stored is None:
        return []
    return [
        str(channel_id) for channel_id in stored.document.rules.bump_channel.channels
    ]


async def admin_scope(session, handler) -> tuple[list[str], bool]:
    """``(channel_ids, empty_means_all)`` for one admin handler row.

    Every handler but the bump tracker: its own ``channel_ids``, where an
    empty list is the whole guild. The bump tracker: the policy's bump
    channels, where an empty list is no channel."""
    if is_bump_tracker(handler):
        return await bump_channel_ids(session, handler.guild_id), False
    return [str(channel_id) for channel_id in handler.channel_ids or []], True
