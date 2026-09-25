"""A media read's whole lifetime fits the bot's memory headroom (#25).

The bot idles ~70 MiB under its 512Mi limit. Before this, one 20 MB image read
grew it by ~102 MiB (the SDK copies the request ~5x), a 10 MB PDF of text ops
by 9.5 GiB (pdfplumber), and reads ran side by side. Now a read downloads at
most ``MAX_DOWNLOAD_BYTES``, sends at most ``MAX_SEND_BYTES``, parses PDFs in a
bounded child, decodes only the text it can use, and runs alone in its process.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFile

from smarter_dev.bot.agents import chat_tools
from smarter_dev.bot.agents import media_reader
from smarter_dev.bot.utils import web_fetch
from smarter_dev.shared import media_images
from smarter_dev.shared import media_reads
from smarter_dev.shared import pdf_text
from smarter_dev.shared.media_images import ImageTooLarge
from smarter_dev.shared.media_images import prepare_image
from smarter_dev.shared.media_reads import MAX_DOWNLOAD_BYTES
from smarter_dev.shared.media_reads import MAX_SEND_BYTES
from smarter_dev.web import media_read

REPO = Path(__file__).parents[3]
MiB = 1024 * 1024


def _encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    out = io.BytesIO()
    block = ImageFile.MAXBLOCK
    # A progressive JPEG of noise needs a larger encoder buffer than the default.
    ImageFile.MAXBLOCK = max(block, 64 * MiB)
    try:
        image.save(out, fmt, **kwargs)
    finally:
        ImageFile.MAXBLOCK = block
    return out.getvalue()


def _noise(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    return Image.frombytes(mode, size, os.urandom(size[0] * size[1] * len(mode)))


def _screenshot(size: tuple[int, int]) -> Image.Image:
    """Lines of text over a dark background, with a photo-like noise panel."""
    image = Image.new("RGB", size, (30, 30, 36))
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], 16):
        draw.text((10, y), "async def read(self): return await fetch(url) " * 8, fill="white")
    image.paste(_noise((size[0] // 2, size[1] // 2)), (size[0] // 3, size[1] // 3))
    return image


def _text_pdf(ops: bytes) -> bytes:
    """A one-page PDF whose content stream is ``ops``."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _pdf_bomb(megabytes: float) -> bytes:
    # One page of text-drawing ops: pdfplumber lays out every character.
    op = b"BT /F1 12 Tf 72 720 Td (lorem ipsum dolor sit amet) Tj ET\n"
    return _text_pdf(op * int(megabytes * MiB / len(op)))


def _long_pdf(pages: int) -> bytes:
    """An ordinary PDF of ``pages`` short pages: slow to parse, cheap to hold."""
    ops = b"BT /F1 12 Tf 72 720 Td (" + b"page text " * 20 + b") Tj ET"
    first_page = 5
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (first_page + i) for i in range(pages)), pages),
        b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ] + [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 3 0 R "
        b"/Resources << /Font << /F1 4 0 R >> >> >>"
    ] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _with_frame_size(jpeg: bytes, width: int, height: int) -> bytes:
    """``jpeg`` with its SOF header claiming ``width`` x ``height``.

    Everything up to the first scan is what the header checks read; the
    scan data after it is never decoded when the image is refused.
    """
    for sof in (b"\xff\xc0", b"\xff\xc2"):
        at = jpeg.find(sof)
        if at != -1:
            break
    head = bytearray(jpeg)
    head[at + 5 : at + 7] = height.to_bytes(2, "big")
    head[at + 7 : at + 9] = width.to_bytes(2, "big")
    return bytes(head)


def _first_scan_of_one_component(jpeg: bytes) -> bytes:
    """A baseline ``jpeg`` whose first scan carries only its first component.

    That is a multi-scan sequential JPEG, which libjpeg buffers whole like a
    progressive one. (jpegtran -scans writes real ones; this rewrites only the
    scan header, which is all the checks read.)
    """
    at = jpeg.find(b"\xff\xda")
    component = jpeg[at + 5 : at + 7]
    scan = b"\xff\xda\x00\x08\x01" + component + b"\x00\x3f\x00"
    length = int.from_bytes(jpeg[at + 2 : at + 4], "big")
    return jpeg[:at] + scan + jpeg[at + 2 + length :]


# --------------------------------------------------------------------------- #
# What one model call may carry
# --------------------------------------------------------------------------- #


def test_large_jpeg_is_drafted_down_not_decoded_whole():
    jpeg = _encode(_noise((4000, 3000)), "JPEG", quality=85)
    assert len(jpeg) > MAX_SEND_BYTES

    [(data, media_type)], note = prepare_image(jpeg, "image/jpeg")

    assert media_type == "image/jpeg" and note == ""
    assert len(data) <= MAX_SEND_BYTES
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 2048


def test_jpeg_coefficient_buffer_is_read_from_the_markers():
    small = _noise((64, 48))
    baseline = _encode(small, "JPEG", quality=85)
    progressive = _encode(small, "JPEG", quality=85, progressive=True)

    assert media_images.jpeg_coefficient_buffer(baseline) == 0
    assert media_images.jpeg_coefficient_buffer(_first_scan_of_one_component(baseline)) > 0
    # 8000 x 8000 at 4:2:0: luma 1000 x 1000 blocks, chroma 500 x 500 each,
    # 64 two-byte coefficients per block.
    assert media_images.jpeg_coefficient_buffer(
        _with_frame_size(progressive, 8000, 8000)
    ) == (1000 * 1000 + 2 * 500 * 500) * 128
    assert media_images.jpeg_coefficient_buffer(b"not a jpeg") is None
    assert media_images.jpeg_coefficient_buffer(progressive[:20]) is None


@pytest.mark.parametrize("layout", ["progressive", "multi-scan"])
def test_large_buffered_jpeg_is_refused_before_decoding(monkeypatch, layout):
    # libjpeg holds every coefficient of such a JPEG whatever the draft
    # scale: an 8000 x 8000 one grew a read by 199 MiB.
    monkeypatch.setattr(media_images, "MAX_SEND_BYTES", 1000)
    small = _noise((64, 48))
    if layout == "progressive":
        jpeg = _with_frame_size(_encode(small, "JPEG", progressive=True), 8000, 8000)
    else:
        jpeg = _first_scan_of_one_component(
            _with_frame_size(_encode(small, "JPEG"), 8000, 8000)
        )

    with pytest.raises(ImageTooLarge, match="8000x8000 pixels"):
        prepare_image(jpeg, "image/jpeg")


def test_buffered_jpeg_within_the_cap_is_still_read():
    jpeg = _encode(_noise((2400, 1800)), "JPEG", quality=95, progressive=True)
    assert len(jpeg) > MAX_SEND_BYTES
    assert 0 < media_images.jpeg_coefficient_buffer(jpeg) <= media_images.MAX_JPEG_BUFFERED_BYTES

    [(data, media_type)], _ = prepare_image(jpeg, "image/jpeg")

    assert media_type == "image/jpeg" and len(data) <= MAX_SEND_BYTES


def test_large_png_screenshot_is_reduced_within_the_pixel_cap():
    png = _encode(_screenshot((2560, 1600)), "PNG")
    png += b"\0" * max(0, MAX_SEND_BYTES + 1 - len(png))  # over the send cap

    [(data, media_type)], _ = prepare_image(png, "image/png")

    assert len(data) <= MAX_SEND_BYTES
    with Image.open(io.BytesIO(data)) as image:
        # Short side kept at or above 768, which is all the model sees.
        assert min(image.size) >= 768 and max(image.size) <= 2048


def test_png_over_the_downscale_cap_is_refused_from_its_header():
    png = _encode(_screenshot((3840, 2160)), "PNG")
    png += b"\0" * max(0, MAX_SEND_BYTES + 1 - len(png))

    with pytest.raises(ImageTooLarge, match="3840x2160 pixels"):
        prepare_image(png, "image/png")


def test_oversized_gif_goes_as_its_first_frame():
    # Over the 8 MB sampling cap and the send cap, under the download cap.
    frames = [_noise((1000, 1000), "L") for _ in range(7)]
    gif = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=40)
    assert media_images.MAX_GIF_BYTES < len(gif) <= MAX_DOWNLOAD_BYTES

    [(data, media_type)], note = prepare_image(gif, "image/gif")

    assert media_type == "image/png" and "only its first frame" in note
    assert len(data) <= MAX_SEND_BYTES


def test_sampled_gif_frames_stop_at_the_send_budget():
    # Four noise frames of ~1 MB PNG each: they cannot all go in one request.
    frames = [_noise((700, 700), "L") for _ in range(4)]
    gif = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=40)
    assert len(gif) <= MAX_SEND_BYTES

    parts, note = prepare_image(gif, "image/gif")

    assert sum(len(data) for data, _ in parts) <= MAX_SEND_BYTES
    assert 2 <= len(parts) < 4 and "animated GIF of 4 frames" in note


def test_small_images_still_pass_through_untouched():
    png = _encode(Image.new("RGB", (64, 64), "blue"), "PNG")
    assert prepare_image(png, "image/png") == ([(png, "image/png")], "")


async def test_audio_over_the_send_cap_is_refused_by_both_readers(monkeypatch):
    clip = b"OggS" + b"\0" * MAX_SEND_BYTES
    agent = MagicMock()
    agent.run = AsyncMock()
    monkeypatch.setattr(media_reader, "get_audio_reader_agent", lambda: agent)
    monkeypatch.setattr(media_read, "_get_audio_agent", lambda: agent)

    for describe in (media_reader.describe_media, media_read._describe_media):
        out = await describe(
            instruction="i", data=clip, media_type="audio/ogg", url="u", kind="audio"
        )
        assert "audio clip is 3.0 MB, too large to read" in out
    agent.run.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #


def _client(status: int, headers: dict, body: bytes, pulled: list[int]):
    class Response:
        status_code = status

        def __init__(self):
            self.headers = headers

        async def aiter_bytes(self):
            for start in range(0, len(body), 65536):
                pulled.append(start)
                yield body[start : start + 65536]

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *exc):
            return False

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return Stream()

    return Client


@pytest.mark.parametrize("module", ["bot", "worker"])
async def test_a_declared_oversized_body_is_refused_before_reading(monkeypatch, module):
    pulled: list[int] = []
    client = _client(200, {"content-length": str(MAX_DOWNLOAD_BYTES + 1)}, b"x", pulled)
    if module == "bot":
        monkeypatch.setattr(web_fetch, "httpx", SimpleNamespace(AsyncClient=client))
        assert await web_fetch.fetch_bytes("https://x/big.png") is None
    else:
        monkeypatch.setattr(media_read.httpx, "AsyncClient", client)
        assert await media_read._fetch_bytes("https://x/big.png") is None
    assert pulled == []


@pytest.mark.parametrize("module", ["bot", "worker"])
async def test_an_undeclared_oversized_body_is_abandoned_at_the_cap(monkeypatch, module):
    pulled: list[int] = []
    body = b"\0" * (MAX_DOWNLOAD_BYTES + 65536)
    client = _client(200, {}, body, pulled)
    if module == "bot":
        monkeypatch.setattr(web_fetch, "httpx", SimpleNamespace(AsyncClient=client))
        assert await web_fetch.fetch_bytes("https://x/big.png") is None
    else:
        monkeypatch.setattr(media_read.httpx, "AsyncClient", client)
        assert await media_read._fetch_bytes("https://x/big.png") is None
    assert len(pulled) <= MAX_DOWNLOAD_BYTES // 65536 + 1


# --------------------------------------------------------------------------- #
# One read at a time, and freed memory handed back
# --------------------------------------------------------------------------- #


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(deps=SimpleNamespace(channel_id=1, bot=None))


async def test_reads_run_one_at_a_time_and_release_memory(monkeypatch):
    running = peak = 0
    released = []

    async def slow_read(url, instruction, ext, log_url):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return {"url": url, "summary": "ok"}

    monkeypatch.setattr(chat_tools, "_read_downloaded", slow_read)
    monkeypatch.setattr(chat_tools, "_post_status", AsyncMock())
    monkeypatch.setattr(media_reads, "release_freed_memory", lambda: released.append(1))
    urls = [f"https://cdn.discordapp.com/attachments/1/{i}/a.png?hm=x" for i in range(4)]

    await asyncio.gather(*(chat_tools.web_read(_ctx(), url, "read") for url in urls))

    assert peak == 1
    assert len(released) == 4


async def test_a_read_that_cannot_get_the_slot_says_the_reader_is_busy(monkeypatch):
    monkeypatch.setattr(media_reads, "SLOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(chat_tools, "_post_status", AsyncMock())
    url = "https://cdn.discordapp.com/attachments/1/2/a.png?hm=x"

    async with media_reads.media_read_slot():
        out = await chat_tools.web_read(_ctx(), url, "read")

    assert out["error"] == "busy"
    assert "Another file is being read" in out["detail"]


# --------------------------------------------------------------------------- #
# Text decodes only what the summary can use
# --------------------------------------------------------------------------- #


def test_long_text_decodes_only_its_usable_prefix():
    # A multi-byte character straddles the cut: it is dropped, not a failure.
    head = b"a" * (chat_tools.MAX_TEXT_DECODE_BYTES - 1) + "é".encode()
    text = chat_tools._decode_text(head + b"b" * MiB, labelled_text=False)

    assert text is not None
    assert len(text) == chat_tools.MAX_TEXT_DECODE_BYTES - 1


def test_long_utf16_text_still_decodes():
    data = "﻿hello\n".encode("utf-16-le") * (chat_tools.MAX_TEXT_DECODE_BYTES // 8)
    text = chat_tools._decode_text(b"\xff\xfe" + data, labelled_text=True)

    assert text is not None and text.lstrip("﻿").startswith("hello")


# --------------------------------------------------------------------------- #
# PDFs parse in a bounded child process
# --------------------------------------------------------------------------- #


async def test_pdf_child_reads_an_ordinary_pdf_and_removes_the_file():
    path = pdf_text.spool(_text_pdf(b"BT /F1 12 Tf 72 720 Td (Quarterly budget: 42) Tj ET"))

    text = await pdf_text.pdf_text_from_file(path, 1000)

    assert "Quarterly budget: 42" in text
    assert not os.path.exists(path)


async def test_pdf_child_refuses_a_layout_bomb_without_touching_the_parent():
    path = pdf_text.spool(_pdf_bomb(2))

    with pytest.raises(pdf_text.PdfUnreadable, match="too large or complex"):
        await pdf_text.pdf_text_from_file(path, 100_000)
    assert not os.path.exists(path)


async def test_pdf_child_is_killed_at_its_time_limit(monkeypatch):
    monkeypatch.setattr(pdf_text, "CHILD_TIMEOUT_SECONDS", 0.01)
    path = pdf_text.spool(_text_pdf(b"BT /F1 12 Tf 72 720 Td (x) Tj ET"))

    with pytest.raises(pdf_text.PdfUnreadable, match="too long"):
        await pdf_text.pdf_text_from_file(path, 1000)
    assert not os.path.exists(path)


def _capture_children(monkeypatch) -> list:
    spawned: list = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(pdf_text.asyncio, "create_subprocess_exec", capture)
    return spawned


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/status") as status:
            return "zombie" not in status.read()
    except FileNotFoundError:
        return False


async def _parsing(spawned: list) -> None:
    for _ in range(500):
        if spawned:
            await asyncio.sleep(0.5)  # well into the parse
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the PDF child never started")


async def test_cancelling_a_pdf_read_kills_and_reaps_its_child(monkeypatch):
    spawned = _capture_children(monkeypatch)
    path = pdf_text.spool(_long_pdf(1500))
    read = asyncio.create_task(pdf_text.pdf_text_from_file(path, 10_000_000))
    await _parsing(spawned)

    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    [child] = spawned
    assert child.returncode is not None
    assert not _alive(child.pid)
    assert not os.path.exists(path)


async def test_a_second_cancel_during_the_kill_still_reaps_the_child(monkeypatch):
    spawned = _capture_children(monkeypatch)
    path = pdf_text.spool(_long_pdf(1500))
    read = asyncio.create_task(pdf_text.pdf_text_from_file(path, 10_000_000))
    await _parsing(spawned)
    [child] = spawned
    wait = child.wait
    reaping = asyncio.Event()
    reaped: list[int] = []

    async def slow_wait():
        reaping.set()
        await asyncio.sleep(0.3)  # the window for a second cancel
        reaped.append(await wait())
        return reaped[-1]

    child.wait = slow_wait

    read.cancel()
    await asyncio.wait_for(reaping.wait(), 10)  # the kill has begun
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    assert reaped, "the read ended before its child was reaped"
    assert child.returncode is not None
    assert not _alive(child.pid)
    assert not os.path.exists(path)


async def test_the_slot_is_not_released_before_the_pdf_child_is_gone(monkeypatch):
    spawned = _capture_children(monkeypatch)
    path = pdf_text.spool(_long_pdf(1500))

    async def read():
        async with media_reads.media_read_slot():
            await pdf_text.pdf_text_from_file(path, 10_000_000)

    task = asyncio.create_task(read())
    await _parsing(spawned)
    task.cancel()
    async with media_reads.media_read_slot(wait=30):
        assert not _alive(spawned[0].pid)
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# Whole reads, measured in a fresh process (media_read_harness): VmHWM from
# before the download, with the real SDK serializing the request. These cover
# only the inputs below, generated here, not production attachments.
# --------------------------------------------------------------------------- #

PEAK_BUDGET_MIB = 40
# A PDF read's parent and parser child at the same instant. The child alone
# is ~35 MiB once pdfplumber is imported; this leaves >10 MiB of the bot's
# ~70 MiB headroom.
PDF_COMBINED_BUDGET_MIB = 60


def _read(tmp_path, name: str, data: bytes, concurrency: int = 1) -> dict:
    path = tmp_path / name
    path.write_bytes(data)
    done = subprocess.run(
        [sys.executable, "-W", "ignore", str(Path(__file__).with_name("media_read_harness.py")),
         str(path), name, str(concurrency)],
        capture_output=True, text=True, check=True, cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYDANTIC_AI_NO_BANNER": "1"},
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.slow
@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux VmHWM")
def test_whole_reads_fit_the_headroom(tmp_path):
    big = [_noise((1000, 1000), "L") for _ in range(7)]  # over the sampling cap
    # 8000 x 8000 progressive and multi-scan JPEGs over the send cap. Their
    # scan data is noise nobody decodes: the header check refuses them first.
    buffered = _with_frame_size(_encode(_noise((64, 48)), "JPEG"), 8000, 8000)
    buffered += os.urandom(4 * MiB)
    progressive = _with_frame_size(
        _encode(_noise((64, 48)), "JPEG", progressive=True), 8000, 8000
    ) + os.urandom(4 * MiB)
    cases = {
        "progressive.jpg": progressive,
        "multiscan.jpg": _first_scan_of_one_component(buffered),
        # At the coefficient cap (19.3 MiB) with a ~9 MB input: the most a
        # progressive JPEG that is read can cost.
        "buffered.jpg": _encode(_noise((3200, 2112)), "JPEG", quality=99, progressive=True),
        "long.pdf": _long_pdf(300),
        "screenshot.png": _encode(_screenshot((2560, 1600)), "PNG"),
        "photo.jpg": _encode(_noise((4000, 3000)), "JPEG", quality=85),
        "big.gif": _encode(big[0], "GIF", save_all=True, append_images=big[1:]),
        "bomb.pdf": _pdf_bomb(9.5),
        "notes.txt": b"lorem ipsum dolor sit amet " * (9 * MiB // 27),
    }
    results = {name: _read(tmp_path, name, data) for name, data in cases.items()}

    assert results["screenshot.png"]["result"] == "described"
    assert results["photo.jpg"]["result"] == "described"
    assert results["big.gif"]["result"] == "described"
    assert results["bomb.pdf"]["error"] == "pdf_read_failed"
    assert results["long.pdf"]["error"] == ""
    assert "too large to read" in results["progressive.jpg"]["result"]
    assert "too large to read" in results["multiscan.jpg"]["result"]
    assert results["buffered.jpg"]["result"] == "described"
    for pdf in ("bomb.pdf", "long.pdf"):
        assert results[pdf]["combined_mib"] <= PDF_COMBINED_BUDGET_MIB, results[pdf]
    # Only the usable prefix is decoded (the summarizer, stubbed here, then
    # keeps MAX_READ_CHARS of it).
    assert results["notes.txt"]["result"] == (
        f"text {chat_tools.MAX_TEXT_DECODE_BYTES} chars"
    )
    for name, result in results.items():
        assert result["growth_mib"] <= PEAK_BUDGET_MIB, (name, result)
        if not name.endswith(".pdf"):  # nothing else starts a process
            assert result["combined_mib"] <= PEAK_BUDGET_MIB, (name, result)


@pytest.mark.slow
@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux VmHWM")
def test_concurrent_reads_do_not_add_up(tmp_path):
    shot = _encode(_screenshot((2560, 1600)), "PNG")

    one = _read(tmp_path, "one.png", shot)
    three = _read(tmp_path, "three.png", shot, concurrency=3)

    assert three["growth_mib"] <= PEAK_BUDGET_MIB
    assert three["growth_mib"] <= one["growth_mib"] + 10
