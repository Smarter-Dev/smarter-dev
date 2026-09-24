"""BMP and animated GIF reach GPT-6 Luna in a form it reads (#11).

Checked against the live API on 2026-09-24: Luna refuses BMP with a 400, and
reads only the first frame of an animated GIF (Gemini 3.8 Flash too). BMP is
re-encoded as PNG; an animated GIF goes as evenly spaced PNG frames with a note.
Static PNG, JPEG, WebP and GIF, and all audio, pass through untouched.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from PIL import Image
from pydantic_ai import BinaryContent

from smarter_dev.bot.agents import chat_tools
from smarter_dev.bot.agents import media_reader
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import web_read
from smarter_dev.shared.media_images import MAX_GIF_FRAMES
from smarter_dev.shared.media_images import prepare_image
from smarter_dev.web import media_read

SIGNED = "?ex=66f3a1b2&is=66f25032&hm=abc123def456&"
BMP_ATTACHMENT = f"https://cdn.discordapp.com/attachments/111/226/scan.bmp{SIGNED}"
GIF_ATTACHMENT = f"https://media.discordapp.net/attachments/111/227/reaction.gif{SIGNED}"


def _encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **kwargs)
    return out.getvalue()


def _bmp() -> bytes:
    return _encode(Image.new("RGB", (8, 4), "red"), "BMP")


def _gif(frames: int) -> bytes:
    # Each frame a distinct solid colour, so the PNG sent for a frame says which.
    images = [Image.new("RGB", (6, 6), (i * 20 % 256, 0, 0)) for i in range(frames)]
    return _encode(
        images[0], "GIF", save_all=True, append_images=images[1:], duration=100, loop=0
    )


def _red_of(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        return image.convert("RGB").getpixel((0, 0))[0]


# --------------------------------------------------------------------------- #
# prepare_image
# --------------------------------------------------------------------------- #


def test_bmp_is_reencoded_as_png():
    parts, note = prepare_image(_bmp(), "image/bmp")

    assert note == ""
    [(data, media_type)] = parts
    assert media_type == "image/png"
    with Image.open(io.BytesIO(data)) as image:
        assert (image.format, image.size) == ("PNG", (8, 4))
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_animated_gif_is_sent_as_evenly_spaced_frames_in_order():
    parts, note = prepare_image(_gif(10), "image/gif")

    assert [media_type for _, media_type in parts] == ["image/png"] * MAX_GIF_FRAMES
    # Frames 1, 4, 7, 10 of 10: first and last always, the rest spread between.
    assert [_red_of(data) for data, _ in parts] == [0, 60, 120, 180]
    assert "animated GIF of 10 frames" in note
    assert "frames 1, 4, 7, 10" in note


def test_short_animated_gif_sends_every_frame():
    parts, note = prepare_image(_gif(2), "image/gif")

    assert [_red_of(data) for data, _ in parts] == [0, 20]
    assert "animated GIF of 2 frames" in note


@pytest.mark.parametrize(
    ("fmt", "media_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_supported_still_images_pass_through_unchanged(fmt, media_type):
    data = _encode(Image.new("RGB", (4, 4), "blue"), fmt)

    assert prepare_image(data, media_type) == ([(data, media_type)], "")


def test_unreadable_bytes_pass_through_for_the_model_to_refuse():
    assert prepare_image(b"BMnot really", "image/bmp") == (
        [(b"BMnot really", "image/bmp")],
        "",
    )
    assert prepare_image(b"GIF89a broken", "image/gif") == (
        [(b"GIF89a broken", "image/gif")],
        "",
    )


# --------------------------------------------------------------------------- #
# Both readers send the prepared parts
# --------------------------------------------------------------------------- #


def _recording_agent(output: str = "described") -> MagicMock:
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(output=output))
    return agent


def _sent(agent: MagicMock) -> tuple[str, list[BinaryContent]]:
    [prompt, *binaries] = agent.run.await_args.args[0]
    return prompt, binaries


@pytest.fixture
def bot_agents(monkeypatch):
    image, audio = _recording_agent("image"), _recording_agent("audio")
    monkeypatch.setattr(media_reader, "get_media_reader_agent", lambda: image)
    monkeypatch.setattr(media_reader, "get_audio_reader_agent", lambda: audio)
    return image, audio


@pytest.fixture
def worker_agents(monkeypatch):
    image, audio = _recording_agent("image"), _recording_agent("audio")
    monkeypatch.setattr(media_read, "_get_media_agent", lambda: image)
    monkeypatch.setattr(media_read, "_get_audio_agent", lambda: audio)
    return image, audio


def _describers(bot_agents, worker_agents):
    return [
        (media_reader.describe_media, bot_agents),
        (media_read._describe_media, worker_agents),
    ]


async def test_both_readers_send_bmp_to_luna_as_png(bot_agents, worker_agents):
    for describe, (image_agent, _) in _describers(bot_agents, worker_agents):
        await describe(
            instruction="read it", data=_bmp(), media_type="image/bmp", url="u", kind="image"
        )
        prompt, [binary] = _sent(image_agent)
        assert binary.media_type == "image/png"
        assert "NOTE:" not in prompt


async def test_both_readers_send_animated_gif_frames_with_the_note(bot_agents, worker_agents):
    for describe, (image_agent, _) in _describers(bot_agents, worker_agents):
        await describe(
            instruction="what happens?", data=_gif(6), media_type="image/gif", url="u", kind="image"
        )
        prompt, binaries = _sent(image_agent)
        assert [b.media_type for b in binaries] == ["image/png"] * MAX_GIF_FRAMES
        assert "INSTRUCTION:\nwhat happens?" in prompt
        assert "NOTE: This is an animated GIF of 6 frames" in prompt


async def test_both_readers_leave_png_and_audio_untouched(bot_agents, worker_agents):
    png = _encode(Image.new("RGB", (4, 4), "blue"), "PNG")
    for describe, (image_agent, audio_agent) in _describers(bot_agents, worker_agents):
        await describe(instruction="i", data=png, media_type="image/png", url="u", kind="image")
        await describe(instruction="i", data=b"OggS...", media_type="audio/ogg", url="u", kind="audio")

        _, [image_binary] = _sent(image_agent)
        _, [audio_binary] = _sent(audio_agent)
        assert (image_binary.data, image_binary.media_type) == (png, "image/png")
        assert (audio_binary.data, audio_binary.media_type) == (b"OggS...", "audio/ogg")


# --------------------------------------------------------------------------- #
# End to end through web_read's signed-attachment path (#92/#94)
# --------------------------------------------------------------------------- #


def _ctx() -> SimpleNamespace:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return SimpleNamespace(deps=ChatDeps(bot=bot, channel_id=1, guild_id=2))


@pytest.mark.parametrize(
    ("url", "data", "expected_parts"),
    [(BMP_ATTACHMENT, _bmp(), 1), (GIF_ATTACHMENT, _gif(5), MAX_GIF_FRAMES)],
)
async def test_signed_attachment_reaches_luna_readable(bot_agents, url, data, expected_parts):
    image_agent, _ = bot_agents
    fetch = AsyncMock(return_value=(data, "application/octet-stream"))
    jina = AsyncMock(side_effect=AssertionError("attachments must not go to Jina"))
    with (
        patch.object(chat_tools.web_fetch, "fetch_bytes", fetch),
        patch.object(chat_tools.web_fetch, "fetch_via_jina", jina),
    ):
        out = await web_read(_ctx(), url, "what is shown?")

    assert out == {"url": url, "kind": "image", "summary": "image"}
    # The signed URL is fetched as given; only the logs drop its query.
    assert fetch.await_args.args[0] == url
    _, binaries = _sent(image_agent)
    assert [b.media_type for b in binaries] == ["image/png"] * expected_parts
