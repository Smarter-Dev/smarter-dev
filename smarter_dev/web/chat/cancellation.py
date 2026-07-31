"""Distributed, event-driven cancellation signals for web Chat agents."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from smarter_dev.shared.redis_client import get_redis_client

_CHANNEL_PREFIX = "smarter-dev:chat:cancellation"


@dataclass(frozen=True, slots=True)
class CancellationNotice:
    reason: str
    subagent_id: UUID | None = None
    worker_lease_token: str | None = None


def cancellation_channel(turn_id: UUID) -> str:
    return f"{_CHANNEL_PREFIX}:{turn_id}"


async def publish_cancellation(
    turn_id: UUID,
    *,
    reason: str,
    subagent_id: UUID | None = None,
    worker_lease_token: str | None = None,
) -> None:
    """Signal active workers after durable cancellation state is committed."""
    payload = json.dumps(
        {
            "reason": reason,
            "subagent_id": str(subagent_id) if subagent_id is not None else None,
            "worker_lease_token": worker_lease_token,
        },
        separators=(",", ":"),
    )
    await get_redis_client().publish(cancellation_channel(turn_id), payload)


def parse_notice(raw: object) -> CancellationNotice | None:
    try:
        payload = json.loads(str(raw))
        raw_subagent_id = payload.get("subagent_id")
        return CancellationNotice(
            reason=str(payload["reason"]),
            subagent_id=UUID(raw_subagent_id) if raw_subagent_id else None,
            worker_lease_token=payload.get("worker_lease_token"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


@asynccontextmanager
async def cancellation_subscription(turn_id: UUID):
    """Subscribe before checking durable state so cancellation cannot race setup."""
    pubsub = get_redis_client().pubsub()
    channel = cancellation_channel(turn_id)
    try:
        await pubsub.subscribe(channel)
        yield pubsub
    finally:
        with suppress(Exception):
            await pubsub.unsubscribe(channel)
        await pubsub.aclose()
