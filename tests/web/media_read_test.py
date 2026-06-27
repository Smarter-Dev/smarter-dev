"""Tests for the worker-tier URL reader (media_read).

Covers routing (image/audio -> describe, pdf -> extract, other -> page text) and
the Redis cache keyed on file content hash + instruction, which is what keeps the
same screenshot from being re-described across many messages.
"""

from __future__ import annotations

import pytest

from smarter_dev.web import media_read


class FakeRedis:
    """Minimal async Redis stand-in: in-memory dict, ignores TTL."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if a test path actually tries to hit the network."""

    async def _boom(*args, **kwargs):
        raise AssertionError("unexpected network fetch")

    monkeypatch.setattr(media_read, "_fetch_bytes", _boom)


async def test_image_url_describes_then_caches(monkeypatch):
    calls: list[bytes] = []

    async def fake_fetch(url):
        return b"PNGDATA", "image/png"

    async def fake_describe(*, instruction, data, media_type, url, kind):
        calls.append(data)
        assert kind == "image"
        assert media_type == "image/png"
        return "a fake crypto trade screenshot"

    monkeypatch.setattr(media_read, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(media_read, "_describe_media", fake_describe)
    redis = FakeRedis()

    first = await media_read.read_url(
        "https://cdn.example/x.png", "describe it", redis=redis
    )
    second = await media_read.read_url(
        "https://cdn.example/x.png", "describe it", redis=redis
    )

    assert first == "a fake crypto trade screenshot"
    assert second == first
    # Same file + same instruction -> described exactly once, served from cache after.
    assert len(calls) == 1


async def test_same_file_different_instruction_redescribes(monkeypatch):
    calls: list[str] = []

    async def fake_fetch(url):
        return b"PNGDATA", "image/png"

    async def fake_describe(*, instruction, data, media_type, url, kind):
        calls.append(instruction)
        return f"answer for: {instruction}"

    monkeypatch.setattr(media_read, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(media_read, "_describe_media", fake_describe)
    redis = FakeRedis()

    a = await media_read.read_url("https://cdn.example/x.png", "what text?", redis=redis)
    b = await media_read.read_url("https://cdn.example/x.png", "any faces?", redis=redis)

    assert a != b
    assert calls == ["what text?", "any faces?"]


async def test_audio_url_routed_to_describe(monkeypatch):
    async def fake_fetch(url):
        return b"OGGDATA", "audio/ogg"

    seen = {}

    async def fake_describe(*, instruction, data, media_type, url, kind):
        seen["kind"] = kind
        seen["media_type"] = media_type
        return "transcript"

    monkeypatch.setattr(media_read, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(media_read, "_describe_media", fake_describe)

    out = await media_read.read_url("https://cdn.example/clip.ogg", "transcribe")
    assert out == "transcript"
    assert seen == {"kind": "audio", "media_type": "audio/ogg"}


async def test_pdf_url_extracts_text_and_caches(monkeypatch):
    extract_calls: list[bytes] = []

    async def fake_fetch(url):
        return b"%PDF-bytes", "application/pdf"

    def fake_extract(data):
        extract_calls.append(data)
        return "  page one text  "

    monkeypatch.setattr(media_read, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(media_read, "_extract_pdf_text", fake_extract)
    redis = FakeRedis()

    # Instruction differs but PDF text is instruction-independent -> extract once.
    first = await media_read.read_url("https://x/doc.pdf", "summarize", redis=redis)
    second = await media_read.read_url("https://x/doc.pdf", "find the date", redis=redis)

    assert first == "page one text"
    assert second == "page one text"
    assert len(extract_calls) == 1


async def test_html_url_uses_jina(monkeypatch, no_network):
    async def fake_jina(client, url):
        return {"title": "Hello", "content": "body text"}

    monkeypatch.setattr(media_read, "jina_read", fake_jina)

    out = await media_read.read_url("https://example.com/article", "read it")
    assert "Hello" in out
    assert "body text" in out


async def test_fetch_failure_returns_error(monkeypatch):
    async def fake_fetch(url):
        return None

    monkeypatch.setattr(media_read, "_fetch_bytes", fake_fetch)

    out = await media_read.read_url("https://cdn.example/x.png", "describe", redis=None)
    assert out.startswith("error:")


async def test_cache_key_combines_file_and_instruction():
    a = media_read._cache_key(b"data", "one")
    b = media_read._cache_key(b"data", "two")
    c = media_read._cache_key(b"other", "one")
    assert a != b  # same file, different instruction
    assert a != c  # different file, same instruction
    assert a == media_read._cache_key(b"data", "one")  # stable


async def test_read_works_without_redis(monkeypatch):
    async def fake_fetch(url):
        return b"PNGDATA", "image/png"

    async def fake_describe(*, instruction, data, media_type, url, kind):
        return "described"

    monkeypatch.setattr(media_read, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(media_read, "_describe_media", fake_describe)

    out = await media_read.read_url("https://cdn.example/x.png", "go", redis=None)
    assert out == "described"
