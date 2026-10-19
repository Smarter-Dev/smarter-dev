"""Wire contract for waking the proactive agent in a newly enabled channel.

``/proactive on`` wakes the guild agent bot-side when it switches a channel
on. The web pages that switch channels on (the ``/configure`` form, the admin
overview) run in another process, so they ask the bot for the same wake with a
:class:`ChannelEnabledRequest` on :data:`CHANNEL_ENABLED_STREAM`. The bot's
proactive control loop reads it and enqueues the very notification
``/proactive on`` does; the request carries only which channel, never the
notification itself.

Shared by the web and the bot, so it imports nothing heavier than Pydantic.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID
from uuid import uuid4

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

CHANNEL_ENABLED_STREAM = "proactive:v1:channel-enabled"
# Processed entries are deleted; this only bounds a backlog nobody reads.
CHANNEL_ENABLED_STREAM_MAXLEN = 1000


class ChannelEnabledRequest(BaseModel):
    """Wake the guild agent: this channel was just switched on."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    command_id: UUID = Field(default_factory=uuid4)
    guild_id: str = Field(pattern=r"^[0-9]{1,20}$")
    channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    created_at: AwareDatetime


async def request_channel_enabled_wake(
    redis, *, guild_id: str, channel_id: str, now
) -> ChannelEnabledRequest:
    """XADD one wake request for the bot's control loop."""
    request = ChannelEnabledRequest(
        guild_id=guild_id, channel_id=channel_id, created_at=now
    )
    await redis.xadd(
        CHANNEL_ENABLED_STREAM,
        {"payload": request.model_dump_json()},
        maxlen=CHANNEL_ENABLED_STREAM_MAXLEN,
        approximate=True,
    )
    return request
