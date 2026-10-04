"""Keep a handler fire's message text out of the worker tables.

Skrift stores a job's payload in ``worker_queue``, in the job's state for a
week and in an open dead letter until someone deals with it. A fire's trigger
context carries what a member wrote, so dispatch puts only the redacted
context in the payload and hands the verbatim one over in Redis for
:data:`FIRE_CONTEXT_TTL_SECONDS`, under a random reference the payload
carries. The fire job reads it back before running the script, so the script
still sees the real message.

A fire that finds the hand-off gone does not run: a script given the
placeholder in place of the message could post or moderate on it. Contexts
with nothing to redact are not handed off at all.
"""

from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

from pydantic_core import to_json

from smarter_dev.shared.message_content import redact_trigger_context

FIRE_CONTEXT_TTL_SECONDS = 60 * 60

_KEY_PREFIX = "handler-fire:context:"


def fire_context_key(context_ref: str) -> str:
    return f"{_KEY_PREFIX}{context_ref}"


async def hand_off_fire_context(redis, context: dict) -> tuple[dict, str | None]:
    """The context a fire payload may carry, and the reference to the rest.

    Returns ``(redacted, None)`` when redaction changes nothing, so a context
    without message text costs no Redis write.
    """
    redacted = redact_trigger_context(context)
    if redacted == context:
        return redacted, None
    context_ref = uuid4().hex
    await redis.set(
        fire_context_key(context_ref),
        to_json(context),
        ex=FIRE_CONTEXT_TTL_SECONDS,
    )
    return redacted, context_ref


async def load_fire_context(
    redis, trigger_context: dict, context_ref: str | None
) -> dict | None:
    """The context to run the script against, or None if the hand-off expired.

    A payload with no reference carries its whole context (nothing needed
    redacting, or it was enqueued before the hand-off existed).
    """
    if context_ref is None:
        return deepcopy(trigger_context)
    raw = await redis.get(fire_context_key(context_ref))
    if raw is None:
        return None
    return json.loads(raw)
