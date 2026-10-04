"""Validation rules of the privacy compaction and the one transcript renderer."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from smarter_dev.bot.agents.chat_input_format import render_message_xml
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import Message
from smarter_dev.bot.privacy.compaction import ID_FEEDBACK
from smarter_dev.bot.privacy.compaction import NAME_FEEDBACK
from smarter_dev.bot.privacy.compaction import PrivacyCompactionFailed
from smarter_dev.bot.privacy.compaction import generate_validated
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.transcript import render_transcript_line
from smarter_dev.bot.proactive.types import ChannelMessage
from smarter_dev.bot.proactive.types import blocked_channel_message
from smarter_dev.shared.privacy_purge import BLOCKED_PLACEHOLDER
from smarter_dev.shared.privacy_purge import PurgeTarget

KAI = "111111111111111111"
TARGET = PurgeTarget.build(KAI, ["kai"])


def _scripted(outputs):
    feedbacks = []

    async def produce(feedback):
        feedbacks.append(feedback)
        output = outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    return produce, feedbacks


async def test_id_hit_retries_twice_then_fails():
    produce, feedbacks = _scripted([f"x {KAI}"] * 5)

    with pytest.raises(PrivacyCompactionFailed) as error:
        await generate_validated(produce, lambda o: (o,), TARGET)

    assert feedbacks == [None, ID_FEEDBACK, ID_FEEDBACK]
    assert KAI not in str(error.value)


async def test_id_hit_then_clean_output_is_accepted():
    produce, feedbacks = _scripted([f"x {KAI}", "nia only"])

    result = await generate_validated(produce, lambda o: (o,), TARGET)

    assert result.output == "nia only" and result.name_hits == 0
    assert result.attempts == 2


async def test_name_hit_gets_exactly_one_reask_then_counts():
    produce, feedbacks = _scripted(["Kai said", "kai again, KAI"])

    result = await generate_validated(produce, lambda o: (o,), TARGET)

    assert feedbacks == [None, NAME_FEEDBACK]
    assert result.output == "kai again, KAI"
    assert result.name_hits == 2


async def test_whole_word_only():
    produce, _ = _scripted(["kaiser rolls and makai"])

    result = await generate_validated(produce, lambda o: (o,), TARGET)

    assert result.attempts == 1 and result.name_hits == 0


async def test_model_errors_use_the_same_budget():
    produce, feedbacks = _scripted([RuntimeError("x")] * 3)

    with pytest.raises(PrivacyCompactionFailed):
        await generate_validated(produce, lambda o: (o,), TARGET)

    assert len(feedbacks) == 3


def _record(**overrides):
    record = {
        "id": "5",
        "timestamp": "2026-10-03T10:00:00+00:00",
        "author_id": "222222222222222222",
        "author_display": "nia",
        "is_bot": False,
        "reply_to_id": "4",
        "content": "hi",
    }
    record.update(overrides)
    return record


def test_transcript_line_carries_uid_and_blocked_form_is_exact():
    assert render_transcript_line(_record(), {"222222222222222222": "A"}) == (
        "[2026-10-03T10:00:00Z] [id=5] A·nia (uid=222222222222222222) "
        "(reply to id=4): hi"
    )
    assert (
        render_transcript_line(_record(blocked=True, author_id=KAI), {})
        == BLOCKED_PLACEHOLDER
    )


def test_blocked_channel_message_round_trips_and_is_not_addressable():
    blocked = blocked_channel_message(datetime(2026, 10, 3, tzinfo=UTC))
    restored = ChannelMessage.from_record(blocked.to_record())
    assert restored.blocked
    env = ChannelEnvironment(visible=[blocked], bot_user_id="9")
    assert env.render([blocked]) == BLOCKED_PLACEHOLDER
    assert env.lookup("") is None
    assert env.slice_around("", radius=3) == []


def test_chat_renderer_routes_blocked_message_to_placeholder():
    me = Me(user_id="9", username="bot")
    message = Message(message_id="", author_id="", body="", blocked=True)
    assert render_message_xml(message, me=me, authors=[]) == BLOCKED_PLACEHOLDER
