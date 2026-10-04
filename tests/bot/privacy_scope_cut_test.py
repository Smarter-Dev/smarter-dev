"""Kept mechanisms after the scope cut: structured acks against the shared
schema, the heartbeat during a long purge, and watch instructions that must
be read (no settings service fails; disabled channels are inspected)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from smarter_dev.bot.privacy import purge
from smarter_dev.bot.privacy.blocked_users import consumer_key
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.services.proactive_settings_service import ProactiveChannelSettings
from tests.bot.privacy_purge_test import PURGE_STREAM
from tests.bot.privacy_purge_test import _command
from tests.bot.privacy_purge_test import _consume_once
from tests.bot.privacy_purge_test import _instruction_store
from tests.bot.privacy_purge_test import _publish
from tests.bot.privacy_purge_test import build_world

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "contracts/privacy/v1/purge_ack.schema.json"
    ).read_text(encoding="utf-8")
)
STRUCTURED = ("name_hits", "tombstoned", "unchecked_names", "done_record")


@pytest.fixture
async def world():
    return await build_world()


def _valid(ack) -> dict:
    payload = ack.model_dump(mode="json", exclude_none=True)
    Draft202012Validator(SCHEMA).validate(payload)
    for field in STRUCTURED:
        assert field in payload, field
    assert "mixed_segments" not in payload
    return payload


async def test_every_ack_path_sends_valid_structured_fields(world):
    # failed (the id survives every retry; nothing written)
    world.summarizer.mode = "leak_id"
    await _publish(world.redis, _command())
    await _consume_once(world)
    failed = _valid(world.acks[-1][1])
    assert failed["outcome"] == "failed" and failed["done_record"] == "not_written"

    # written (fresh purge)
    world.summarizer.mode = "good"
    await _publish(world.redis, _command())
    await _consume_once(world)
    written = _valid(world.acks[-1][1])
    assert written["done_record"] == "written"
    assert written["tombstoned"] is False

    # replayed (redelivery of a finished guild after a failed ack post)
    posts = []

    async def flaky(run_id, ack):
        posts.append(ack)
        if len(posts) == 1:
            raise ConnectionError("web down")
        world.acks.append((run_id, ack))
        return True

    world.deps.post_ack = flaky
    stream_id = await _publish(world.redis, _command())
    await _consume_once(world)
    entries = await world.redis.xrange(PURGE_STREAM, min=stream_id, max=stream_id)
    await purge.process_entry(world.deps, stream_id, entries[0][1])
    replayed = _valid(world.acks[-1][1])
    assert replayed["done_record"] == "replayed"


async def test_heartbeat_is_renewed_during_a_long_purge(world, monkeypatch):
    monkeypatch.setattr(purge, "CLAIM_RENEW_SECONDS", 0.01)

    async def slow_ack(run_id, ack):
        await world.redis.delete(consumer_key())
        await asyncio.sleep(0.05)
        world.acks.append((run_id, ack))
        return True

    world.deps.post_ack = slow_ack
    await _publish(world.redis, _command())
    entries = await purge.read_batch(world.redis, "c", block_ms=10)
    for stream_id, fields in entries:
        await purge.process_entry(world.deps, stream_id, fields, consumer="c")

    assert await world.redis.get(consumer_key()) == b"1"


async def test_no_settings_service_fails_the_watch_step(world):
    world.run.bot.d.pop("proactive_settings_service")
    await _publish(world.redis, _command())

    await _consume_once(world)

    ack = world.acks[0][1]
    assert ack.outcome == "failed"
    assert "SettingsServiceUnavailable" in ack.detail


async def test_disabled_channels_watch_instructions_are_purged(world):
    disabled = 77
    stored = _instruction_store().to_stored()
    saved = {}

    async def get_settings(guild_id, channel_id):
        return ProactiveChannelSettings(
            guild_id=guild_id, channel_id=channel_id, enabled=False,
            watch_addendum=stored if channel_id == str(disabled) else "",
        )

    async def set_watch_addendum(guild_id, channel_id, addendum):
        saved[channel_id] = addendum

    world.settings.get_settings = get_settings
    world.settings.set_watch_addendum = set_watch_addendum
    world.run.bot.cache = SimpleNamespace(
        get_guilds_view=lambda: {},
        get_guild_channels_view_for_guild=lambda gid: {disabled: object()},
    )
    await _publish(world.redis, _command())

    await _consume_once(world)

    assert str(disabled) in saved
    entries = InstructionStore.from_stored("seed", saved[str(disabled)]).entries
    assert [e.instruction_id for e in entries] == ["w2"]


async def test_reaction_preview_redacts_and_drops_replies_to_blocked():
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.privacy.blocked_users import get_blocked_users

    kai = "111111111111111111"
    get_blocked_users().load(1, [kai])
    bot_reply_to_kai = SimpleNamespace(
        content=f"<@{kai}> thanks for the numbers",
        referenced_message=SimpleNamespace(author=SimpleNamespace(id=int(kai))),
    )
    bot_note = SimpleNamespace(content=f"pinging <@{kai}> and nia", referenced_message=None)

    assert proactive._reaction_preview(bot_reply_to_kai) == ""
    assert proactive._reaction_preview(bot_note) == "pinging @[blocked user] and nia"


def test_reply_notification_omits_a_bot_reply_to_a_blocked_member():
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.proactive.notifications import reply_notification
    from smarter_dev.bot.proactive.types import ChannelMessage

    def message(id_, content, **extra):
        return ChannelMessage(
            id=id_, timestamp=datetime.now(UTC), author_id="222222222222222222",
            author_name="nia", author_display="nia", is_bot=False, content=content,
            reply_to_id=None, mention_user_ids=(), mention_everyone=False,
            attachment_count=0, sticker_count=0, message_type=0, **extra,
        )

    replied = message("4", "kai, your secret benchmark", replies_to_blocked=True)
    body = reply_notification(message("5", "agreed"), replied).body

    assert "secret benchmark" not in body and "id=4" in body
