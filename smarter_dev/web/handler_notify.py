"""Post a channel notice when a handler fire errors, so people know to fix it.

A failed fire is otherwise invisible to the channel — it's only in the durable
HandlerRun audit. This surfaces it where the members who asked for the handler
will see it. Throttled hard (one notice per handler per window via a shared Redis
counter) because a broken handler errors on *every* fire and must not spam.

Best-effort: a notice that can't be posted is logged, never raised — the fire's
own outcome and audit record stand regardless.
"""

from __future__ import annotations

import logging

from smarter_dev.web.handler_caps import (
    ERROR_NOTICES_PER_WINDOW,
    WindowedLimiter,
    handler_error_notice_key,
)
from smarter_dev.web.handler_emitter import DiscordEmitter

logger = logging.getLogger(__name__)

MAX_ERROR_DETAIL = 300


def format_error_notice(error: str | None) -> str:
    """A short, single-block channel message describing the failure."""
    detail = " ".join((error or "unknown error").split())
    if len(detail) > MAX_ERROR_DETAIL:
        detail = detail[: MAX_ERROR_DETAIL - 1] + "…"
    return (
        "⚠️ A handler in this channel hit an error and didn't run. "
        "An admin can review and fix it from the dashboard.\n"
        f"```\n{detail}\n```"
    )


async def notify_handler_error(
    *,
    emitter: DiscordEmitter,
    limiter: WindowedLimiter,
    handler_id: str,
    channel_id: str,
    error: str | None,
) -> bool:
    """Post a throttled error notice to ``channel_id``. Returns whether it posted.

    ``limiter`` should use the error-notice window so the per-handler key bounds
    notices to one per window even though the handler may error every fire.
    """
    if not channel_id:
        return False
    try:
        within = await limiter.hit(
            handler_error_notice_key(handler_id), ERROR_NOTICES_PER_WINDOW
        )
        if not within:
            return False
        await emitter.create_message(channel_id, format_error_notice(error))
        return True
    except Exception:  # noqa: BLE001 — a notice must never break the fire's audit
        logger.warning(
            "failed to post handler error notice (channel=%s)", channel_id, exc_info=True
        )
        return False
