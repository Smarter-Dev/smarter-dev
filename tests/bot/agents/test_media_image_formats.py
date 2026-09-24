"""BMP and animated GIF reach GPT-6 Luna in a form it reads (#11).

Checked against the live API on 2026-09-24: Luna refuses BMP with a 400, and
reads only the first frame of an animated GIF (Gemini 3.8 Flash too). BMP is
re-encoded as PNG; an animated GIF goes as evenly spaced PNG frames with a note.
Static PNG, JPEG, WebP and GIF, and all audio, pass through untouched.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
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
from smarter_dev.shared import media_images
from smarter_dev.shared.media_images import MAX_GIF_FRAMES
from smarter_dev.shared.media_images import ImageTooLarge
from smarter_dev.shared.media_images import prepare_image
from smarter_dev.shared.media_images import scan_gif
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


def test_rgb_frames_stay_rgb_and_large_ones_are_scaled_down(monkeypatch):
    monkeypatch.setattr(media_images, "MAX_SIDE", 4)
    parts, _ = prepare_image(_encode(Image.new("RGB", (8, 2), "red"), "BMP"), "image/bmp")

    with Image.open(io.BytesIO(parts[0][0])) as image:
        assert (image.mode, image.size) == ("RGB", (4, 1))


# --------------------------------------------------------------------------- #
# Memory bounds: nothing over a cap is decoded; it goes as it arrived
# --------------------------------------------------------------------------- #


def _claiming(data: bytes, offset: int, fmt: str, width: int, height: int) -> bytes:
    """``data`` with its header rewritten to claim ``width`` x ``height``."""
    return data[:offset] + struct.pack(fmt, width, height) + data[offset + struct.calcsize(fmt):]


def test_bmp_over_the_pixel_cap_is_refused_from_its_header():
    # The reviewer's 863 MB case: 12000 x 12000. The body is 8 x 4, so reaching
    # the decoder would fail differently; the header alone must refuse it.
    huge = _claiming(_bmp(), 18, "<ii", 12000, 12000)

    with pytest.raises(ImageTooLarge, match="12000x12000 pixels, too large to read"):
        prepare_image(huge, "image/bmp")


def test_gif_over_the_pixel_cap_is_read_from_its_first_frame():
    # The reviewer's 476 MB case: 6000 x 6000 frames.
    huge = _claiming(_gif(40), 6, "<HH", 6000, 6000)

    assert prepare_image(huge, "image/gif") == ([(huge, "image/gif")], "")


def test_only_bmp_reaches_a_decoder(monkeypatch):
    opened = []
    real_open = media_images.Image.open
    monkeypatch.setattr(
        media_images.Image, "open", lambda *a, **kw: opened.append(kw) or real_open(*a, **kw)
    )

    prepare_image(_encode(Image.new("RGB", (4, 4)), "TIFF"), "image/tiff")
    prepare_image(_bmp(), "image/bmp")
    prepare_image(_gif(3), "image/gif")

    assert opened == [{"formats": ["BMP"]}, {"formats": ["GIF"]}]


def test_gif_over_the_byte_cap_is_read_from_its_first_frame(monkeypatch):
    gif = _gif(3)
    monkeypatch.setattr(media_images, "MAX_GIF_BYTES", len(gif) - 1)

    assert prepare_image(gif, "image/gif") == ([(gif, "image/gif")], "")


def test_gif_over_the_frame_pixel_budget_is_read_from_its_first_frame(monkeypatch):
    gif = _gif(10)  # 10 frames x 36 pixels
    monkeypatch.setattr(media_images, "MAX_GIF_DECODE_PIXELS", 359)

    assert prepare_image(gif, "image/gif") == ([(gif, "image/gif")], "")


@pytest.mark.parametrize("frames", [1, 2, 7, 37])
def test_scan_gif_matches_pillow_without_decoding(frames):
    gif = _gif(frames)
    with Image.open(io.BytesIO(gif)) as image:
        assert scan_gif(gif) == (image.n_frames, 36)
        assert image.n_frames == frames


def test_scan_gif_stops_counting_at_the_frame_limit(monkeypatch):
    monkeypatch.setattr(media_images, "MAX_GIF_COUNT", 6)
    gif = _gif(7)

    assert scan_gif(gif) is None
    assert prepare_image(gif, "image/gif") == ([(gif, "image/gif")], "")


def test_gif_frame_larger_than_its_canvas_is_not_decoded():
    # The first image descriptor follows the 13-byte header, the colour table
    # and any extensions; point its width and height at 6000 x 6000.
    gif = _gif(3)
    descriptor = gif.index(b"\x2c\x00\x00\x00\x00")
    huge = _claiming(gif, descriptor + 5, "<HH", 6000, 6000)

    assert scan_gif(huge)[1] == 36_000_000
    assert prepare_image(huge, "image/gif") == ([(huge, "image/gif")], "")


def test_scan_gif_rejects_malformed_gifs():
    gif = _gif(5)
    assert scan_gif(gif[:-10]) is None
    assert scan_gif(b"GIF89a" + b"\x00" * 20) is None
    assert scan_gif(b"\x89PNG\r\n\x1a\n") is None


def _placed(gif: bytes, frame: int, x0: int, y0: int) -> bytes:
    """``gif`` with its ``frame``-th image descriptor moved to (x0, y0)."""
    at = -1
    for _ in range(frame + 1):
        at = gif.index(b"\x2c", at + 1)
        while gif[at + 5 : at + 9] != struct.pack("<HH", 10, 10):  # a 10x10 frame
            at = gif.index(b"\x2c", at + 1)
    return gif[: at + 1] + struct.pack("<HH", x0, y0) + gif[at + 5 :]


def _ten_by_ten(frames: int) -> bytes:
    images = [Image.new("RGB", (10, 10), (i * 40, 0, 0)) for i in range(frames)]
    return _encode(images[0], "GIF", save_all=True, append_images=images[1:], duration=50)


@pytest.mark.parametrize("frame", [0, 1])
def test_a_frame_placed_past_the_canvas_grows_it_and_is_not_decoded(frame):
    # Pillow enlarges the canvas to x0 + width by y0 + height while seeking; the
    # review's probe: 10 x 10 canvas, a frame at (6000, 6000), +339 MiB decoded.
    gif = _placed(_ten_by_ten(2), frame, 6000, 6000)

    assert scan_gif(gif) == (2, 6010 * 6010)
    assert prepare_image(gif, "image/gif") == ([(gif, "image/gif")], "")


async def test_conversions_run_one_at_a_time(monkeypatch):
    running = peak = 0

    def slow_prepare(data, media_type):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        time.sleep(0.05)
        running -= 1
        return [(data, media_type)], ""

    monkeypatch.setattr(media_images, "prepare_image", slow_prepare)
    await asyncio.gather(
        *(media_images.prepare_image_bounded(_bmp(), "image/bmp") for _ in range(4))
    )

    assert peak == 1


async def test_images_needing_no_work_skip_the_queue(monkeypatch):
    monkeypatch.setattr(
        media_images, "prepare_image", MagicMock(side_effect=AssertionError("no work"))
    )
    png = _encode(Image.new("RGB", (4, 4)), "PNG")

    assert await media_images.prepare_image_bounded(png, "image/png") == (
        [(png, "image/png")],
        "",
    )


# --------------------------------------------------------------------------- #
# Peak memory, measured in a fresh process at the caps, for these inputs only.
# The request body here is the base64 JSON built below; the SDK's own
# serialization (pydantic-ai, the OpenAI client, httpx) makes further copies
# that this does not include. VmHWM, not ru_maxrss:
# Linux carries ru_maxrss across exec, so a child of this (large) test process
# would start at its parent's peak and show no growth at all. tracemalloc misses
# Pillow's allocations. The baseline is taken before the input is read, and
# every copy is held to the end: the input, the PNG parts, and their base64
# request body.
# --------------------------------------------------------------------------- #

_MEASURE = """
import base64, json, sys
from smarter_dev.shared import media_images
def hwm_kib():
    for line in open("/proc/self/status"):
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
path, media_type = sys.argv[1], sys.argv[2]
base = hwm_kib()
data = open(path, "rb").read()
try:
    parts, note = media_images.prepare_image(data, media_type)
except media_images.ImageTooLarge:
    parts, note = [], "too large"
body = json.dumps({"input": [
    "data:%s;base64,%s" % (mt, base64.b64encode(part).decode()) for part, mt in parts
]}).encode()
peak = hwm_kib()
print(json.dumps({"growth_mib": (peak - base) / 1024, "types": [mt for _, mt in parts],
                  "input_mb": len(data) / 1e6, "body_mb": len(body) / 1e6}))
"""
PEAK_BUDGET_MIB = 40


def _measure(tmp_path, name: str, data: bytes, media_type: str) -> dict:
    path = tmp_path / name
    path.write_bytes(data)
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", _MEASURE, str(path), media_type],
        capture_output=True, text=True, check=True, cwd=Path(__file__).parents[3],
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _noise(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    return Image.frombytes(mode, size, os.urandom(size[0] * size[1] * len(mode)))


@pytest.mark.slow
@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux VmHWM")
def test_peak_memory_at_the_caps(tmp_path):
    side = int(media_images.MAX_DECODE_PIXELS**0.5)  # 1414: just under the cap
    # A 32-bit noise BMP at the pixel cap, padded to the 20 MB fetch cap.
    bmp = _encode(_noise((side, side), "RGBA"), "BMP")
    bmp += b"\0" * (20 * 1024 * 1024 - len(bmp))
    # A GIF at the per-frame and total-decode caps: 25 frames of 2M pixels.
    frames = media_images.MAX_GIF_DECODE_PIXELS // (side * side)
    base = Image.linear_gradient("L").resize((side, side))
    gif = _encode(
        base.convert("RGB"), "GIF", save_all=True, duration=40,
        append_images=[base.point(lambda v, i=i: (v + i * 9) % 256).convert("RGB")
                       for i in range(1, frames)],
    )
    # Noisy frames, as large as the byte cap allows, for the largest PNGs.
    noisy = _encode(
        _noise((side, side), "L"), "GIF", save_all=True, duration=40,
        append_images=[_noise((side, side), "L") for _ in range(2)],
    )
    assert len(noisy) <= media_images.MAX_GIF_BYTES

    results = {
        "bmp": _measure(tmp_path, "cap.bmp", bmp, "image/bmp"),
        "gif": _measure(tmp_path, "cap.gif", gif, "image/gif"),
        "noisy_gif": _measure(tmp_path, "noisy.gif", noisy, "image/gif"),
    }

    assert results["bmp"]["types"] == ["image/png"]
    assert results["gif"]["types"] == ["image/png"] * MAX_GIF_FRAMES
    # Three 1000 x 1000 noise frames do not fit one request under #25's
    # MAX_SEND_BYTES, and a GIF over it goes as its first frame.
    assert results["noisy_gif"]["types"] == ["image/png"]
    for name, result in results.items():
        assert result["growth_mib"] <= PEAK_BUDGET_MIB, (name, result)


def _canvas_gif(width: int, height: int, frames: int) -> bytes:
    """A real GIF of 1x1 frames on a ``width`` x ``height`` canvas.

    Pillow composites every frame onto the full canvas, so this 940-byte file
    at 6000 x 6000 x 40 grows an unguarded decode by ~310 MiB — the review's
    476 MB GIF case without spending two minutes encoding 36M-pixel frames.
    """
    out = bytearray(b"GIF89a" + struct.pack("<HH", width, height) + bytes([0x80, 0, 0]))
    out += bytes([0, 0, 0, 255, 255, 255])  # two-colour global table
    for _ in range(frames):
        out += bytes([0x21, 0xF9, 4, 0x04, 5, 0, 0, 0])  # 50 ms, keep the frame
        out += b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00"
        out += bytes([2, 2, 0x44, 0x01, 0])  # LZW: clear, index 0, end
    return bytes(out + b"\x3b")


def test_canvas_gif_is_a_real_animation():
    with Image.open(io.BytesIO(_canvas_gif(6000, 6000, 40))) as image:
        assert (image.size, image.n_frames) == ((6000, 6000), 40)


@pytest.mark.slow
@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux VmHWM")
def test_reproduced_inputs_are_not_decoded(tmp_path):
    # The review's cases, 863 MB and 476 MB peak before this change.
    bmp = _encode(Image.new("1", (12000, 12000), 1), "BMP")

    refused = _measure(tmp_path, "rev.bmp", bmp, "image/bmp")
    passed = _measure(tmp_path, "rev.gif", _canvas_gif(6000, 6000, 40), "image/gif")
    # The second review's probe: a 10 x 10 GIF whose later frame sits at
    # (11000, 11000), which grew an unguarded decode by 1098 MiB.
    placed = _measure(
        tmp_path, "placed.gif", _placed(_ten_by_ten(2), 1, 11000, 11000), "image/gif"
    )

    # The input itself is held (18 MB for the BMP); nothing is decoded.
    assert refused["types"] == []
    assert refused["growth_mib"] < refused["input_mb"] + 5
    assert passed["types"] == ["image/gif"] and passed["growth_mib"] < 5
    assert placed["types"] == ["image/gif"] and placed["growth_mib"] < 5


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


async def test_both_readers_answer_a_too_large_bmp_without_a_model_call(
    bot_agents, worker_agents
):
    huge = _claiming(_bmp(), 18, "<ii", 12000, 12000)
    for describe, (image_agent, _) in _describers(bot_agents, worker_agents):
        out = await describe(
            instruction="read it", data=huge, media_type="image/bmp", url="u", kind="image"
        )
        assert out.startswith("The image is 12000x12000 pixels, too large to read")
        image_agent.run.assert_not_awaited()


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
