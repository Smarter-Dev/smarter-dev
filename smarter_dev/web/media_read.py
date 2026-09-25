"""Worker-tier URL reader for handlers — any URL: image, audio, PDF, or web page.

Mirrors the chat agent's ``web_read`` capability but kept self-contained in the
web/worker tier (no bot-package imports): images are described by GPT-6 Luna
(BMP re-encoded and animated GIFs sampled by ``shared.media_images``, as in the
bot) and audio by Gemini 3.8 Flash (the OpenAI Responses API takes no audio input), PDFs
are extracted with pdfplumber in a bounded child process, everything else is read as page text via Jina.

Media describes (the expensive part — a model call) are cached in Redis keyed on
the **file's content hash + the instruction**, so the same screenshot posted
across many messages is only read once. Caching is best-effort: if Redis is
unavailable the read still works, just uncached.
"""

from __future__ import annotations

import hashlib
import logging
import os

import httpx

from smarter_dev.shared import pdf_text
from smarter_dev.shared.media_images import ImageTooLarge
from smarter_dev.shared.media_images import prepare_image_bounded
from smarter_dev.shared.media_reads import MAX_DOWNLOAD_BYTES
from smarter_dev.shared.media_reads import MAX_SEND_BYTES
from smarter_dev.shared.media_reads import MediaReaderBusy
from smarter_dev.shared.media_reads import SpooledImage
from smarter_dev.shared.media_reads import media_read_slot
from smarter_dev.shared.media_reads import read_body
from smarter_dev.shared.media_reads import too_large_to_send
from smarter_dev.web.research_tools import jina_read

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 100_000
MAX_FETCH_BYTES = MAX_DOWNLOAD_BYTES  # don't download enormous files to read them
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
_audio_agent = None


def _url_extension(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    last = path.rsplit("/", 1)[-1]
    dot = last.rfind(".")
    return last[dot:].lower() if dot != -1 else ""


def _cache_key(data: bytes | SpooledImage, instruction: str) -> str:
    if isinstance(data, SpooledImage):
        digest = hashlib.sha256()
        with open(data.path, "rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        file_hash = digest.hexdigest()
    else:
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


async def _fetch_bytes(
    url: str, *, image: bool = False
) -> tuple[bytes | SpooledImage, str] | None:
    """Download a URL's bytes (capped). Returns (data, content_type) or None.

    Streamed, so a body over ``MAX_FETCH_BYTES`` is abandoned at the cap (or
    refused from its Content-Length) instead of being downloaded whole first.
    An ``image`` over it streams on to disk instead (``SpooledImage``, up to
    ``MAX_IMAGE_DOWNLOAD_BYTES``), to be downsampled from there.
    """
    body = None
    try:
        async with (
            httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                return None
            body = await read_body(
                resp.aiter_bytes(),
                resp.headers.get("content-length", ""),
                max_bytes=MAX_FETCH_BYTES,
                spill_image=image,
            )
            if body is None:
                return None
            return body, resp.headers.get("content-type", "")
    except BaseException as e:
        # Closing the stream or client can still fail or be cancelled after
        # read_body handed the spool over: it is ours to delete.
        if isinstance(body, SpooledImage):
            body.discard()
        if not isinstance(e, Exception):
            raise
        logger.debug("media fetch failed for %s", url, exc_info=True)
        return None


# Images moved from Gemini 3.1 Flash Lite to GPT-6 Luna on 2026-09-24; audio
# went to Gemini 3.8 Flash instead, since Luna cannot take it. Same env vars and
# defaults as the bot's ``media_reader``.
def _get_media_agent():
    global _media_agent
    if _media_agent is None:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIResponsesModel
        from pydantic_ai.models.openai import OpenAIResponsesModelSettings
        from pydantic_ai.providers.openai import OpenAIProvider

        model_id = os.getenv("MEDIA_READER_MODEL", "gpt-6-luna")
        _media_agent = Agent(
            OpenAIResponsesModel(
                model_id,
                provider=OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY") or ""),
            ),
            output_type=str,
            system_prompt=_MEDIA_SYSTEM_PROMPT,
            model_settings=OpenAIResponsesModelSettings(openai_reasoning_effort="low"),
        )
    return _media_agent


def _get_audio_agent():
    global _audio_agent
    if _audio_agent is None:
        from pydantic_ai import Agent
        from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
        from pydantic_ai.providers.google import GoogleProvider

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        model_id = os.getenv("MEDIA_READER_AUDIO_MODEL", "gemini-3.8-flash")
        _audio_agent = Agent(
            GoogleModel(model_id, provider=GoogleProvider(api_key=api_key)),
            output_type=str,
            system_prompt=_MEDIA_SYSTEM_PROMPT,
            model_settings=GoogleModelSettings(
                google_thinking_config={"thinking_level": "LOW"}
            ),
        )
    return _audio_agent


async def _describe_media(
    *,
    instruction: str,
    data: bytes | SpooledImage,
    media_type: str,
    url: str,
    kind: str,
) -> str:
    from pydantic_ai import BinaryContent

    is_audio = kind == "audio" or media_type.startswith("audio/")
    agent = _get_audio_agent() if is_audio else _get_media_agent()
    prompt = f"URL: {url}\nKIND: {kind}\n\nINSTRUCTION:\n{instruction}"
    if is_audio:
        if len(data) > MAX_SEND_BYTES:
            return too_large_to_send(len(data), "audio clip")
        parts = [(data, media_type)]
    else:
        # BMP -> PNG, animated GIF -> sampled frames (see media_images).
        try:
            parts, note = await prepare_image_bounded(data, media_type)
        except ImageTooLarge as too_large:
            return str(too_large)
        if note:
            prompt += f"\n\nNOTE: {note}"
    result = await agent.run(
        [prompt, *(BinaryContent(data=part, media_type=mt) for part, mt in parts)]
    )
    return str(result.output)


async def _extract_pdf_text(path: str) -> str:
    return await pdf_text.pdf_text_from_file(path, MAX_READ_CHARS)


async def read_url(url: str, instruction: str, *, redis=None) -> str:
    """Read any URL into instruction-guided text. Images/audio/PDF are cached by
    content hash + instruction so the same file isn't re-read across messages."""
    ext = _url_extension(url)
    if ext in IMAGE_EXTS or ext in AUDIO_EXTS or ext == ".pdf":
        # One download-to-reply read at a time in this process (#25).
        try:
            async with media_read_slot():
                return await _read_downloaded(url, instruction, ext, redis)
        except MediaReaderBusy as busy:
            return f"error: {busy}"

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


async def _read_downloaded(url: str, instruction: str, ext: str, redis) -> str:
    if ext in IMAGE_EXTS or ext in AUDIO_EXTS:
        kind = "image" if ext in IMAGE_EXTS else "audio"
        # An image over MAX_FETCH_BYTES arrives on disk, to be downsampled.
        fetched = await _fetch_bytes(url, image=kind == "image")
        if fetched is None:
            return f"error: could not fetch {url}"
        data, content_type = fetched
        try:
            media_type = _EXT_MEDIA_TYPE.get(ext) or content_type
            if not media_type:
                return f"error: unknown media type for {url}"
            key = _cache_key(data, instruction)
            cached = await _cache_get(redis, key)
            if cached is not None:
                return cached
            desc = await _describe_media(
                instruction=instruction, data=data, media_type=media_type, url=url, kind=kind
            )
            await _cache_set(redis, key, desc)
            return desc
        finally:
            if isinstance(data, SpooledImage):
                data.discard()

    if ext == ".pdf":
        fetched = await _fetch_bytes(url)
        if fetched is None:
            return f"error: could not fetch {url}"
        data, _ = fetched
        key = _cache_key(data, "__pdf_text__")  # raw text is instruction-independent
        cached = await _cache_get(redis, key)
        if cached is not None:
            return cached
        # Parsed in a bounded child process (shared.pdf_text); drop our bytes
        # first so the two are never held at once.
        path = pdf_text.spool(data)
        del data, fetched
        try:
            text = (await _extract_pdf_text(path)).strip()
        except pdf_text.PdfUnreadable as e:
            return f"error: {e}"
        if not text:
            return f"error: no readable text in {url}"
        text = text[:MAX_READ_CHARS]
        await _cache_set(redis, key, text)
        return text
    raise AssertionError(f"not a downloaded read: {ext!r}")
