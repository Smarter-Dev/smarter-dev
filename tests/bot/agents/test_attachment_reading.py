"""Chat bot attachment visibility and reading (#20).

Covers what the model is shown (the rendered ``<message>`` XML, including a
reply to a file outside the history window), what the response gate judges,
and how ``web_read`` reads Discord's signed attachment URLs: fetched
directly, routed on the actual bytes, bounded, and never logged with their
signature.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import httpx
import pytest

from smarter_dev.bot.agents import chat_context
from smarter_dev.bot.agents import chat_tools
from smarter_dev.bot.agents.chat_input_format import render_message_xml
from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import web_read
from smarter_dev.bot.services.chat_engine import ChannelEngine
from smarter_dev.bot.utils import web_fetch

SIGNATURE = "hm=abc123def456"
PDF_URL = (
    "https://cdn.discordapp.com/attachments/111/222/spec.pdf"
    f"?ex=66f3a1b2&is=66f25032&{SIGNATURE}&"
)
LOG_URL = (
    "https://cdn.discordapp.com/attachments/111/223/app.log"
    f"?ex=66f3a1b2&is=66f25032&{SIGNATURE}&"
)
ZIP_URL = (
    "https://cdn.discordapp.com/attachments/111/224/bundle.zip"
    f"?ex=66f3a1b2&is=66f25032&{SIGNATURE}&"
)
PNG_URL = (
    "https://media.discordapp.net/attachments/111/225/shot.png"
    f"?ex=66f3a1b2&is=66f25032&{SIGNATURE}&"
)


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF drawing ``text``, with a correct xref table."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
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


def _ctx() -> SimpleNamespace:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return SimpleNamespace(deps=ChatDeps(bot=bot, channel_id=1, guild_id=2))


def _summarizer():
    return patch.object(
        chat_tools,
        "summarize_web_content",
        AsyncMock(side_effect=lambda **kw: f"summary of: {kw['content'][:60]}"),
    )


def _no_jina():
    return patch.object(
        chat_tools.web_fetch,
        "fetch_via_jina",
        AsyncMock(side_effect=AssertionError("attachments must not go to Jina")),
    )


@pytest.mark.asyncio
async def test_signed_pdf_attachment_is_parsed_locally_not_sent_to_jina():
    fetch = AsyncMock(return_value=(_minimal_pdf("Quarterly latency budget"), "application/pdf"))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina(), _summarizer() as s:
        out = await web_read(_ctx(), PDF_URL, "what budget?")

    assert fetch.call_args.args[0] == PDF_URL  # signed query preserved
    assert "Quarterly latency budget" in s.call_args.kwargs["content"]
    assert out["summary"].startswith("summary of:")
    assert "error" not in out


@pytest.mark.asyncio
async def test_pdf_labelled_attachment_that_is_not_a_pdf_is_rejected():
    fetch = AsyncMock(return_value=(b"<html>expired</html>", "application/pdf"))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina():
        out = await web_read(_ctx(), PDF_URL, "read")
    assert out["error"] == "content_mismatch"


@pytest.mark.asyncio
async def test_text_attachment_is_decoded_directly():
    body = b"ERROR worker crashed: KeyError 'token'\n"
    fetch = AsyncMock(return_value=(body, "text/plain"))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina(), _summarizer() as s:
        out = await web_read(_ctx(), LOG_URL, "what failed?")

    assert s.call_args.kwargs["content"] == body.decode()
    assert "error" not in out


@pytest.mark.asyncio
async def test_binary_attachment_reports_the_limit_instead_of_guessing():
    fetch = AsyncMock(return_value=(b"PK\x03\x04\x00\x00binary", "application/zip"))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina():
        out = await web_read(_ctx(), ZIP_URL, "what's inside?")

    assert out["error"] == "unsupported_attachment_type"
    assert "application/zip" in out["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        "application/zip",
        "application/gzip",
        "application/x-7z-compressed",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/x-sqlite3",
        "application/x-msdownload",
        "video/mp4",
    ],
)
async def test_binary_labels_are_refused_even_when_bytes_decode_as_utf8(content_type):
    # No NUL and valid UTF-8, but labelled as a binary format: the label wins.
    fetch = AsyncMock(return_value=(b"PK\x03\x04binary", content_type))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina():
        out = await web_read(_ctx(), ZIP_URL, "what's inside?")
    assert out["error"] == "unsupported_attachment_type"
    assert content_type in out["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "content_type", "expected"),
    [
        # PowerShell `>` writes UTF-16LE with a BOM.
        ("\ufeffERROR build failed\r\n".encode("utf-16-le"), "text/plain", "ERROR build failed"),
        # Excel "CSV" export in Windows-1252.
        ("name,city\nJosé,Montréal\n".encode("cp1252"), "text/csv", "José,Montréal"),
        # A charset parameter on the label does not change the routing.
        ("a;b\nMünchen;1\n".encode("cp1252"), "text/csv; charset=windows-1252", "München"),
        # UTF-32 with a BOM.
        ("\ufeffhello utf32".encode("utf-32-le"), "text/plain", "hello utf32"),
        # Unlabelled but clean UTF-8 text is still read.
        (b"fn main() {}\n", "application/octet-stream", "fn main() {}"),
        # Text under a non-text/ label Discord may assign by extension.
        (b'{"ok": true}', "application/json", '"ok"'),
        (b"Write-Host hi\n", "application/x-powershell", "Write-Host hi"),
        (b"\\section{Intro}\n", "application/x-tex", "\\section{Intro}"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "<svg"),
    ],
)
async def test_text_in_other_encodings_is_decoded(data, content_type, expected):
    fetch = AsyncMock(return_value=(data, content_type))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina(), _summarizer() as s:
        out = await web_read(_ctx(), LOG_URL, "read")
    assert "error" not in out
    assert expected in s.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_non_text_label_does_not_get_the_windows_1252_fallback():
    fetch = AsyncMock(return_value=("José".encode("cp1252"), "application/x-powershell"))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina():
        out = await web_read(_ctx(), LOG_URL, "read")
    assert out["error"] == "unsupported_attachment_type"


@pytest.mark.asyncio
async def test_unlabelled_non_utf8_bytes_are_refused():
    fetch = AsyncMock(return_value=(bytes(range(128, 256)), "application/octet-stream"))
    with patch.object(chat_tools.web_fetch, "fetch_bytes", fetch), _no_jina():
        out = await web_read(_ctx(), ZIP_URL, "read")
    assert out["error"] == "unsupported_attachment_type"


@pytest.mark.asyncio
async def test_image_attachment_goes_to_the_media_reader_when_bytes_are_an_image():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with (
        patch.object(chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=(png, "image/png"))),
        patch.object(chat_tools, "describe_media", AsyncMock(return_value="a stack trace")) as dm,
        _no_jina(),
    ):
        out = await web_read(_ctx(), PNG_URL, "what error?")

    assert out == {"url": PNG_URL, "kind": "image", "summary": "a stack trace"}
    assert dm.call_args.kwargs["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_image_attachment_with_non_image_bytes_is_a_mismatch():
    with (
        patch.object(
            chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=(b"<html>", "image/png"))
        ),
        patch.object(chat_tools, "describe_media", AsyncMock()) as dm,
    ):
        out = await web_read(_ctx(), PNG_URL, "what?")
    assert out["error"] == "content_mismatch"
    dm.assert_not_called()


@pytest.mark.asyncio
async def test_audio_attachment_with_non_audio_bytes_is_a_mismatch():
    url = PNG_URL.replace("shot.png", "voice-message.ogg")
    with (
        patch.object(
            chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=(b"<html>", "audio/ogg"))
        ),
        patch.object(chat_tools, "describe_media", AsyncMock()) as dm,
    ):
        out = await web_read(_ctx(), url, "transcribe")
    assert out["error"] == "content_mismatch"
    dm.assert_not_called()


@pytest.mark.asyncio
async def test_ogg_voice_attachment_goes_to_the_media_reader():
    url = PNG_URL.replace("shot.png", "voice-message.ogg")
    with (
        patch.object(
            chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=(b"OggS\x00rest", "video/ogg"))
        ),
        patch.object(chat_tools, "describe_media", AsyncMock(return_value="hello")) as dm,
    ):
        out = await web_read(_ctx(), url, "transcribe")
    assert out == {"url": url, "kind": "audio", "summary": "hello"}
    assert dm.call_args.kwargs["media_type"] == "audio/ogg"


@pytest.mark.asyncio
async def test_attachment_reads_never_log_the_signature(caplog):
    caplog.set_level(logging.DEBUG)
    with (
        patch.object(chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=None)),
    ):
        out = await web_read(_ctx(), LOG_URL, "read")

    assert out["error"] == "fetch_failed"
    assert "app.log" in caplog.text
    assert SIGNATURE not in caplog.text


@pytest.mark.asyncio
async def test_fetch_bytes_abandons_a_body_over_the_cap(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert SIGNATURE in str(request.url)
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        web_fetch.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    assert await web_fetch.fetch_bytes(LOG_URL, max_bytes=4096) is None
    assert await web_fetch.fetch_bytes(LOG_URL, max_bytes=8192) == (b"x" * 5000, "text/plain")


@pytest.mark.asyncio
async def test_reply_to_a_file_outside_the_window_shows_that_file(monkeypatch):
    monkeypatch.setattr(chat_context, "resolve_mentions", AsyncMock(side_effect=lambda b, *_: b))
    monkeypatch.setattr(chat_context, "_build_authors", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_context, "_build_channel_info", AsyncMock(return_value=None))

    alice = SimpleNamespace(id=2, username="alice")
    old_upload = SimpleNamespace(
        id=10,
        author=alice,
        attachments=[
            SimpleNamespace(url=PDF_URL, media_type="application/pdf", filename="spec.pdf", size=2048)
        ],
    )
    reply = SimpleNamespace(
        id=50,
        author=SimpleNamespace(id=3, username="bob"),
        content="does section 2 still apply?",
        referenced_message=old_upload,
        reactions=[],
        user_mentions_ids=[],
        attachments=[],
        created_at=None,
    )
    bot = MagicMock()
    bot.get_me.return_value = SimpleNamespace(id=999, username="bot")

    messages, _, _, me = await chat_context._convert(
        bot=bot, channel_id=1, guild_id=2, raw_messages=[reply]
    )
    xml = render_message_xml(messages[0], me=me, authors=[Author(user_id="3", username="bob")])

    assert (
        '<attachment kind="pdf" filename="spec.pdf" type="application/pdf" '
        'size="2048" on-message="10" url="https://cdn.discordapp.com/attachments/'
        '111/222/spec.pdf?ex=66f3a1b2&amp;is=66f25032&amp;hm=abc123def456&amp;"/>'
    ) in xml


@pytest.mark.asyncio
async def test_reply_to_a_file_inside_the_window_is_not_repeated(monkeypatch):
    monkeypatch.setattr(chat_context, "resolve_mentions", AsyncMock(side_effect=lambda b, *_: b))
    monkeypatch.setattr(chat_context, "_build_authors", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_context, "_build_channel_info", AsyncMock(return_value=None))

    attachment = SimpleNamespace(url=PDF_URL, media_type="application/pdf", filename="spec.pdf", size=1)
    upload = SimpleNamespace(
        id=10, author=SimpleNamespace(id=2, username="alice"), content="", referenced_message=None,
        reactions=[], user_mentions_ids=[], attachments=[attachment], created_at=None,
    )
    reply = SimpleNamespace(
        id=50, author=SimpleNamespace(id=3, username="bob"), content="?", referenced_message=upload,
        reactions=[], user_mentions_ids=[], attachments=[], created_at=None,
    )
    bot = MagicMock()
    bot.get_me.return_value = SimpleNamespace(id=999, username="bot")

    messages, _, _, _ = await chat_context._convert(
        bot=bot, channel_id=1, guild_id=2, raw_messages=[upload, reply]
    )
    assert [len(m.attachments) for m in messages] == [1, 0]
    assert messages[1].reply_to_attachments == []


def test_gate_sees_attachment_only_messages_as_files_not_empty_text():
    message = SimpleNamespace(
        id=7,
        author=SimpleNamespace(username="alice"),
        content="",
        attachments=[SimpleNamespace(filename="trace.png", media_type="image/png", url=PNG_URL)],
    )
    gate_message = ChannelEngine._to_gate_message(message)
    assert gate_message.content == "[attachment: trace.png, image/png]"
    assert PNG_URL not in gate_message.content
