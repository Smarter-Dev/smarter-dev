#!/usr/bin/env python3
"""One-off: bring Redis keys written before the 6-hour in-flight bound onto it.

Keys keep the expiry they were given when written, so after the deploy that
shortened these bounds the older keys would outlive them by up to two days.
This only ever moves an expiry earlier (``LT``), so it is safe to run more
than once, and while the bot and the worker are running:

- a claimed proactive batch expires ``IN_FLIGHT_MAX`` after its oldest
  envelope was written; a batch with no readable envelope time is deleted;
- ``mediaread:*`` keys expire within ``CACHE_TTL_SECONDS``;
- the chat agent's topic keys expire within ``TOPIC_TTL_SECONDS``.

Prints counts only, never a key. Run it once, right after the deploy, as the
Job in ``k8s/oneoff-cap-inflight-ttls.yaml``. Locally:
    .venv/bin/python scripts/cap_inflight_ttls.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

from redis.asyncio import Redis

from smarter_dev.bot.proactive.redis_queue import KEY_PREFIX as PROACTIVE_PREFIX
from smarter_dev.bot.services.chat_memory import KEY_PREFIX as CHAT_PREFIX
from smarter_dev.bot.services.chat_memory import TOPIC_TTL_SECONDS
from smarter_dev.shared.config import get_settings
from smarter_dev.shared.retention_policy import IN_FLIGHT_MAX_MILLISECONDS
from smarter_dev.web.media_read import CACHE_TTL_SECONDS


def _oldest_created_ms(values: list[bytes]) -> int | None:
    times = []
    for value in values:
        try:
            created_at = json.loads(value)["created_at"]
            times.append(datetime.fromisoformat(created_at).timestamp())
        except (ValueError, KeyError, TypeError):
            continue
    return int(min(times) * 1000) if times else None


async def _cap_batches(redis: Redis) -> dict[str, int]:
    counts = {"batches_capped": 0, "batches_deleted": 0}
    async for key in redis.scan_iter(match=f"{PROACTIVE_PREFIX}:*:batch:*", count=500):
        if key.endswith(b":dropped") or await redis.type(key) != b"list":
            continue
        oldest = _oldest_created_ms(await redis.lrange(key, 0, -1))
        if oldest is None:
            await redis.delete(key)
            counts["batches_deleted"] += 1
        else:
            await redis.pexpireat(key, oldest + IN_FLIGHT_MAX_MILLISECONDS, lt=True)
            counts["batches_capped"] += 1
    return counts


async def _cap(redis: Redis, pattern: str, seconds: int) -> int:
    capped = 0
    async for key in redis.scan_iter(match=pattern, count=500):
        await redis.expire(key, seconds, lt=True)
        capped += 1
    return capped


async def main() -> int:
    redis = Redis.from_url(get_settings().effective_redis_url)
    try:
        counts = await _cap_batches(redis)
        counts["mediaread"] = await _cap(redis, "mediaread:*", CACHE_TTL_SECONDS)
        counts["topic"] = await _cap(redis, f"{CHAT_PREFIX}:*:topic", TOPIC_TTL_SECONDS)
        counts["topic_ts"] = await _cap(
            redis, f"{CHAT_PREFIX}:*:topic_ts", TOPIC_TTL_SECONDS
        )
    finally:
        await redis.aclose()
    print(" ".join(f"{name}={count}" for name, count in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
