"""Tests for graceful degradation when a generated image can't be attached.

If the bot lacks the Discord "Attach Files" permission (or the upload otherwise
fails), the reply must still land — the text answer should be resent without the
attachment rather than the whole message being lost. See the prod incident where
a 403 on the image upload swallowed the entire reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import hikari

from smarter_dev.bot.agents.chat_tools import GeneratedImage
from smarter_dev.bot.services.chat_engine import ChannelEngine, MAX_NO_RESPONSE_TURNS


def _forbidden() -> hikari.ForbiddenError:
    return hikari.ForbiddenError("https://discord/api", {}, "Missing Permissions",
                                 "Missing Permissions", 50013)


def _engine(create_message: AsyncMock) -> ChannelEngine:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = create_message

    async def _noop_voice(*a, **k):
        return None

    async def _noop_deactivate(_):
        return None

    return ChannelEngine(
        bot=bot, channel_id=42, guild_id=99,
        voice_send=_noop_voice, on_deactivate=_noop_deactivate,
    )


def _img() -> GeneratedImage:
    return GeneratedImage(data=b"PNGDATA", mime_type="image/png", filename="diagram.png")


async def test_send_text_falls_back_to_text_only_when_attachment_forbidden():
    calls = []

    async def create_message(channel_id, **kwargs):
        calls.append(kwargs)
        if "attachments" in kwargs:  # first attempt carries the image
            raise _forbidden()
        return MagicMock()  # text-only retry succeeds

    engine = _engine(AsyncMock(side_effect=create_message))
    ok = await engine._send_text("here's the diagram", reply_to=None, images=[_img()])

    assert ok is True
    assert len(calls) == 2
    assert "attachments" in calls[0]
    assert "attachments" not in calls[1]
    # The user still gets the answer, plus a note about the missing permission.
    assert calls[1]["content"].startswith("here's the diagram")
    assert "Attach Files" in calls[1]["content"]


async def test_send_text_fallback_has_no_note_for_generic_failure():
    calls = []

    async def create_message(channel_id, **kwargs):
        calls.append(kwargs)
        if "attachments" in kwargs:
            raise RuntimeError("network blip")
        return MagicMock()

    engine = _engine(AsyncMock(side_effect=create_message))
    ok = await engine._send_text("the answer", reply_to=None, images=[_img()])

    assert ok is True
    assert len(calls) == 2
    # Non-permission failure: text still lands, but no permission note appended.
    assert calls[1]["content"] == "the answer"
    assert "Attach Files" not in calls[1]["content"]


async def test_send_text_without_images_does_not_retry():
    calls = []

    async def create_message(channel_id, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("boom")

    engine = _engine(AsyncMock(side_effect=create_message))
    ok = await engine._send_text("plain text", reply_to=None, images=None)

    assert ok is False
    assert len(calls) == 1  # no fallback loop when there was no attachment


async def test_post_images_notes_missing_permission():
    calls = []

    async def create_message(channel_id, **kwargs):
        calls.append(kwargs)
        if "attachments" in kwargs:
            raise _forbidden()
        return MagicMock()

    engine = _engine(AsyncMock(side_effect=create_message))
    ok = await engine._post_images([_img()], reply_to=None)

    assert ok is False  # the image itself didn't post
    # But a permission note was sent so the turn isn't silent.
    assert len(calls) == 2
    assert "Attach Files" in calls[1]["content"]


# -- image-only turn should not burn a no-response strike -----------------


def _no_response_output() -> SimpleNamespace:
    return SimpleNamespace(
        response=None, topic="topic", notes=None, continue_watching=True
    )


async def _apply(engine: ChannelEngine, output, images):
    memory = MagicMock()
    memory.write_topic = AsyncMock()
    memory.write_notes = AsyncMock()
    with patch(
        "smarter_dev.bot.services.chat_engine.get_chat_memory",
        return_value=memory,
    ):
        return await engine._apply_output(output, images)


async def test_image_only_turn_does_not_burn_a_no_response_strike():
    engine = _engine(AsyncMock(return_value=MagicMock()))  # image posts fine
    engine.consecutive_no_response = MAX_NO_RESPONSE_TURNS - 1  # one before cutoff

    outcome = await _apply(engine, _no_response_output(), [_img()])

    # Posting the image is engagement — the strike counter resets, no deactivation.
    assert engine.consecutive_no_response == 0
    assert outcome.deactivate_reason is None


async def test_image_only_turn_that_fails_to_post_counts_as_silence():
    engine = _engine(AsyncMock(side_effect=RuntimeError("no perms")))
    engine.consecutive_no_response = MAX_NO_RESPONSE_TURNS - 1

    outcome = await _apply(engine, _no_response_output(), [_img()])

    # Nothing was delivered, so it IS a no-response turn and trips the cutoff.
    assert engine.consecutive_no_response == MAX_NO_RESPONSE_TURNS
    assert outcome.deactivate_reason == "no_response_quota"


async def test_no_image_no_response_still_counts_as_silence():
    engine = _engine(AsyncMock(return_value=MagicMock()))
    engine.consecutive_no_response = 0

    outcome = await _apply(engine, _no_response_output(), [])

    assert engine.consecutive_no_response == 1
    assert outcome.deactivate_reason is None
