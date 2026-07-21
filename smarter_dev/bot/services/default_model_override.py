"""Temporary bot-wide default chat model override, stored in Redis.

The admin ``/chat-default-model-override`` command points the *default* chat
model (the one used by every channel without its own ``/chat-bot-settings``
override) at a different catalog model until a UTC end date. The value lives
in a single Redis key whose ``EXAT`` is the end date, so the override expires
on its own and survives bot restarts — the same time-boxed-switch pattern as
the per-channel free-fallback window in :mod:`channel_token_budget`.

Per-channel overrides always win: the chat engine consults this key only when
no channel override chose the turn's model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

DEFAULT_MODEL_OVERRIDE_KEY = "chat-default-model-override"


@dataclass(frozen=True)
class DefaultModelOverride:
    """The temporary default-model choice and when it ends.

    ``model_key`` is a catalog key (not a wire id); ``reasoning_level`` is a
    :class:`~smarter_dev.shared.model_catalog.ReasoningLevel` wire value or
    ``None`` for the model's default. ``expires_at_epoch`` is the UTC epoch
    second the override ends (also the Redis key's ``EXAT``).
    """

    model_key: str
    reasoning_level: str | None
    expires_at_epoch: int


def parse_end_date_utc(text: str, now: datetime) -> datetime:
    """Parse the command's end-date input into an aware UTC datetime.

    Accepts ``YYYY-MM-DD HH:MM`` (that exact UTC minute) or ``YYYY-MM-DD``
    (the override lasts through the end of that UTC day). Raises ``ValueError``
    with a user-facing message on a malformed input or an end in the past.
    """
    cleaned = text.strip()
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            date_only = datetime.strptime(cleaned, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError(
                "End date must be `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` (UTC), "
                f"got `{cleaned}`."
            ) from exc
        # A bare date means "through the end of that UTC day".
        parsed = date_only + timedelta(days=1)
    if parsed <= now:
        raise ValueError(
            f"End date `{cleaned}` is not in the future (it's currently "
            f"{now:%Y-%m-%d %H:%M} UTC)."
        )
    return parsed


async def set_default_model_override(
    redis: Redis, override: DefaultModelOverride
) -> None:
    """Store ``override``, expiring it at its own ``expires_at_epoch``."""
    payload = json.dumps(
        {
            "model_key": override.model_key,
            "reasoning_level": override.reasoning_level,
            "expires_at_epoch": override.expires_at_epoch,
        }
    )
    await redis.set(
        DEFAULT_MODEL_OVERRIDE_KEY, payload, exat=override.expires_at_epoch
    )


async def read_default_model_override(redis: Redis) -> DefaultModelOverride | None:
    """The active temporary default override, or ``None`` when there is none.

    Best-effort: a chat turn must never break because this read failed, so
    Redis trouble or a corrupt/stale payload degrades to "no override" (Redis
    expiry itself handles the time bound). Validation of the stored
    ``model_key`` against the catalog is the caller's job — the engine already
    treats an unknown key as "use the default".
    """
    try:
        raw = await redis.get(DEFAULT_MODEL_OVERRIDE_KEY)
    except (RedisError, TypeError):
        logger.debug("could not read the default-model override", exc_info=True)
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        return DefaultModelOverride(
            model_key=payload["model_key"],
            reasoning_level=payload.get("reasoning_level"),
            expires_at_epoch=int(payload["expires_at_epoch"]),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning(
            "Corrupt default-model override payload %r — ignoring it", raw
        )
        return None
