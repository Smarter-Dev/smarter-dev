"""Tests for the handler data model — single-listener keying in particular."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import ChannelHandler


def _handler(trigger_type: str, channel_id: str = "C1") -> ChannelHandler:
    return ChannelHandler(
        guild_id="G1",
        channel_id=channel_id,
        trigger_type=trigger_type,
        settings={},
        description="d",
        script="await send_message('hi')\n",
        created_by="U1",
    )


async def test_event_trigger_is_single_listener_per_channel(db_session):
    db_session.add(_handler("message"))
    await db_session.commit()
    db_session.add(_handler("message"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_different_event_triggers_coexist(db_session):
    db_session.add(_handler("message"))
    db_session.add(_handler("reaction"))
    await db_session.commit()  # no conflict — different trigger_type


async def test_many_time_triggers_share_a_channel(db_session):
    db_session.add(_handler("timer"))
    db_session.add(_handler("timer"))
    db_session.add(_handler("schedule"))
    await db_session.commit()  # partial index excludes time triggers
