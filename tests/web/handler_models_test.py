"""Tests for the handler data model — name uniqueness keying in particular."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import AdminHandler, ChannelHandler


def _handler(
    trigger_type: str, channel_id: str = "C1", name: str = "helper"
) -> ChannelHandler:
    return ChannelHandler(
        guild_id="G1",
        channel_id=channel_id,
        name=name,
        trigger_type=trigger_type,
        settings={},
        description="d",
        script="await send_message('hi')\n",
        created_by="U1",
    )


async def test_same_trigger_handlers_coexist_under_different_names(db_session):
    db_session.add(_handler("message", name="greeter"))
    db_session.add(_handler("message", name="mood-tracker"))
    await db_session.commit()  # multiple listeners per (channel, trigger) are fine


async def test_name_is_unique_per_channel(db_session):
    db_session.add(_handler("message", name="greeter"))
    await db_session.commit()
    db_session.add(_handler("reaction", name="greeter"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_same_name_in_different_channels(db_session):
    db_session.add(_handler("message", channel_id="C1", name="greeter"))
    db_session.add(_handler("message", channel_id="C2", name="greeter"))
    await db_session.commit()  # uniqueness is per channel


async def test_admin_handler_name_is_unique_per_guild(db_session):
    def _admin(guild_id: str, name: str) -> AdminHandler:
        return AdminHandler(
            guild_id=guild_id,
            name=name,
            trigger_type="message",
            settings={},
            channel_ids=[],
            description="d",
            script="pass\n",
            created_by_admin="A1",
        )

    db_session.add(_admin("G1", "scam-banner"))
    db_session.add(_admin("G2", "scam-banner"))  # other guild — fine
    await db_session.commit()
    db_session.add(_admin("G1", "scam-banner"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
