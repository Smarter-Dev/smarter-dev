"""Worker-tier URL reader for handlers — any URL: image, audio, PDF, or web page.

Mirrors the chat agent's ``web_read`` capability but kept self-contained in the
web/worker tier (no bot-package imports): images/audio are described by a Gemini
multimodal agent, PDFs are extracted with pdfplumber, everything else is read as
page text via Jina.

Media describes (the expensive part — a Gemini call) are cached in Redis keyed on
the **file's content hash + the instruction**, so the same screenshot posted
across many messages is only read once. Caching is best-effort: if Redis is
unavailable the read still works, just uncached.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os

import httpx

from smarter_dev.web.research_tools import jina_read

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 100_000
MAX_FETCH_BYTES = 20 * 1024 * 1024  # don't download enormous files to read them
CACHE_TTL_SECONDS = 24 * 60 * 60

# Extension -> media type, trusting the extension over the server's Content-Type
# (CDNs are unreliable for media, e.g. serving .ogg as video/ogg).
_EXT_MEDIA_TYPE = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".flac": "audio/flac", ".aac": "audio/aac",
}
IMAGE_EXTS = tuple(e for e, mt in _EXT_MEDIA_TYPE.items() if mt.startswith("image/"))
AUDIO_EXTS = tuple(e for e, mt in _EXT_MEDIA_TYPE.items() if mt.startswith("audio/"))

_MEDIA_SYSTEM_PROMPT = """\
You examine a single attached media file — an image or an audio clip — to satisfy a specific
INSTRUCTION from another assistant. Obey the INSTRUCTION precisely. For an IMAGE: describe only
what is actually visible — objects, people, UI, diagrams, charts, and any readable text;
transcribe on-screen text accurately when relevant. For AUDIO: transcribe or summarize what is
said/heard. Stay grounded — never invent details not present. Be concise (at most ~5 paragraphs).
If the media can't be meaningfully read (blank, corrupt, or it doesn't contain what the
INSTRUCTION asks for), say so plainly rather than fabricating."""

_media_agent = None


def _url_extension(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    last = path.rsplit("/", 1)[-1]
    dot = last.rfind(".")
    return last[dot:].lower() if dot != -1 else ""


def _cache_key(data: bytes, instruction: str) -> str:
    file_hash = hashlib.sha256(data).hexdigest()
    instr_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16]
    return f"mediaread:{file_hash}:{instr_hash}"


async def _cache_get(redis, key: str) -> str | None:
    if redis is None:
        return None
    try:
        return await redis.get(key)
    except Exception:  # noqa: BLE001 — cache is best-effort
        return None


async def _cache_set(redis, key: str, value: str) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, value, ex=CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 — cache is best-effort
        logger.debug("media read cache set failed", exc_info=True)


async def _fetch_bytes(url: str) -> tuple[bytes, str] | None:
    """Download a URL's bytes (capped). Returns (data, content_type) or None."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code >= 400 or len(resp.content) > MAX_FETCH_BYTES:
            return None
        return resp.content, resp.headers.get("content-type", "")
    except Exception:  # noqa: BLE001
        logger.debug("media fetch failed for %s", url, exc_info=True)
        return None


def _get_media_agent():
    global _media_agent
    if _media_agent is None:
        from pydantic_ai import Agent
        from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
        from pydantic_ai.providers.google import GoogleProvider

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        model_id = os.getenv("MEDIA_READER_MODEL", "gemini-3.1-flash-lite")
        _media_agent = Agent(
            GoogleModel(model_id, provider=GoogleProvider(api_key=api_key)),
            output_type=str,
            system_prompt=_MEDIA_SYSTEM_PROMPT,
            model_settings=GoogleModelSettings(
                google_thinking_config={"thinking_level": "LOW"}
            ),
        )
    return _media_agent


async def _describe_media(
    *, instruction: str, data: bytes, media_type: str, url: str, kind: str
) -> str:
    from pydantic_ai import BinaryContent

    agent = _get_media_agent()
    prompt = f"URL: {url}\nKIND: {kind}\n\nINSTRUCTION:\n{instruction}"
    result = await agent.run([prompt, BinaryContent(data=data, media_type=media_type)])
    return str(result.output)


def _extract_pdf_text(data: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


async def read_url(url: str, instruction: str, *, redis=None) -> str:
    """Read any URL into instruction-guided text. Images/audio/PDF are cached by
    content hash + instruction so the same file isn't re-read across messages."""
    ext = _url_extension(url)

    if ext in IMAGE_EXTS or ext in AUDIO_EXTS:
        fetched = await _fetch_bytes(url)
        if fetched is None:
            return f"error: could not fetch {url}"
        data, content_type = fetched
        media_type = _EXT_MEDIA_TYPE.get(ext) or content_type
        if not media_type:
            return f"error: unknown media type for {url}"
        kind = "image" if ext in IMAGE_EXTS else "audio"
        key = _cache_key(data, instruction)
        cached = await _cache_get(redis, key)
        if cached is not None:
            return cached
        desc = await _describe_media(
            instruction=instruction, data=data, media_type=media_type, url=url, kind=kind
        )
        await _cache_set(redis, key, desc)
        return desc

    if ext == ".pdf":
        fetched = await _fetch_bytes(url)
        if fetched is None:
            return f"error: could not fetch {url}"
        data, _ = fetched
        key = _cache_key(data, "__pdf_text__")  # raw text is instruction-independent
        cached = await _cache_get(redis, key)
        if cached is not None:
            return cached
        text = _extract_pdf_text(data).strip()
        if not text:
            return f"error: no readable text in {url}"
        text = text[:MAX_READ_CHARS]
        await _cache_set(redis, key, text)
        return text

    async with httpx.AsyncClient(timeout=30.0) as client:
        data = await jina_read(client, url)
    if "error" in data:
        return f"error: {data['error']}"
    content = (data.get("content") or "").strip()
    if not content:
        return f"error: no readable content at {url}"
    title = data.get("title", "")
    header = f"Title: {title}\n\n" if title else ""
    return f"{header}{content[:MAX_READ_CHARS]}"
