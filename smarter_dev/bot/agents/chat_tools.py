"""Tool implementations for the chat agent.

These are bound to a per-turn context (bot + channel + guild) via
``create_chat_tools()``. The resulting list is registered with the
Pydantic AI Agent as its tool surface.
"""

from __future__ import annotations

import codecs
import logging
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import Any

import hikari
import httpx
import pydantic_monty as monty
from pydantic_ai import RunContext

from smarter_dev.bot.agents.image_generator import (
    generate_image as generate_image_bytes,
)
from smarter_dev.bot.agents.image_prompt_reviewer import review_image_prompt
from smarter_dev.bot.agents.media_reader import describe_media
from smarter_dev.bot.agents.url_registry import resolve_escaped_url
from smarter_dev.bot.agents.web_summarizer import summarize_web_content
from smarter_dev.bot.utils import web_fetch
from smarter_dev.shared import pdf_text
from smarter_dev.shared.config import get_settings
from smarter_dev.shared.guild_event_log import chat_memory_enabled
from smarter_dev.shared.media_reads import MAX_DOWNLOAD_BYTES
from smarter_dev.shared.media_reads import MediaReaderBusy
from smarter_dev.shared.media_reads import media_read_slot
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS
from smarter_dev.web.research_tools import brave_search
from smarter_dev.web.search_previews import mark_search_preview_failed
from smarter_dev.web.search_previews import populate_search_preview
from smarter_dev.web.search_previews import reserve_search_preview

# Reads longer than this are truncated before summarization to bound the
# summarizer's input; shorter reads are passed through whole.
MAX_READ_CHARS = 100_000

# Resource limits for the sandboxed run_code tool. No network/filesystem access
# is exposed, and max_duration_secs bounds runaway loops so a turn can't hang.
MONTY_LIMITS: dict[str, Any] = {
    "max_memory": 256 * 1024 * 1024,
    "max_recursion_depth": 500,
    "max_duration_secs": 10.0,
}
# Cap run_code output fed back to the agent so a big print can't flood context.
MAX_CODE_OUTPUT_CHARS = 10_000

# URL extensions routed to the multimodal media reader instead of text
# extraction, mapped to the media type we hand the model. We trust the
# extension over the server's Content-Type, which is unreliable for these
# (e.g. Discord/CDNs serve .ogg as "video/ogg", which the model then rejects).
_EXT_MEDIA_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}
IMAGE_EXTS = tuple(e for e, mt in _EXT_MEDIA_TYPE.items() if mt.startswith("image/"))
AUDIO_EXTS = tuple(e for e, mt in _EXT_MEDIA_TYPE.items() if mt.startswith("audio/"))


def _url_extension(url: str) -> str:
    """Lowercase file extension from a URL path, ignoring query/fragment."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    last = path.rsplit("/", 1)[-1]
    dot = last.rfind(".")
    return last[dot:].lower() if dot != -1 else ""



logger = logging.getLogger(__name__)

COMMON_UNICODE_EMOJIS = [
    "👍", "👎", "❤️", "😀", "😂", "🤔", "🎉", "🔥", "✨",
    "😍", "🙏", "👀", "💯", "🤷", "🚀", "✅", "❌",
]


@dataclass
class GeneratedImage:
    """An image produced by ``generate_image`` this turn, awaiting attachment.

    The engine drains ``ChatDeps.pending_images`` after the run and attaches
    each to the reply message it sends.
    """

    data: bytes
    mime_type: str
    filename: str
    channel_id: str = ""


@dataclass
class ChatDeps:
    """Per-run dependencies injected into chat agent tool calls."""

    bot: Any  # hikari.GatewayBot — typed as Any so tests can pass a mock
    channel_id: int
    guild_id: int
    # APIClient for the handler-management + image-quota tools; built from
    # settings on demand when not supplied (see handler_tools / _bot_api).
    api_client: Any = None
    # Channel name, denormalised onto every memory note so the nightly dream can
    # say "#dev-help" without ever talking to Discord.
    channel_name: str | None = None
    # UUID of the engagement this run belongs to, soft-linked onto saved notes.
    engagement_id: Any = None
    # Images generated this turn, drained by the engine and attached to the
    # outgoing reply. Fresh per run (a new ChatDeps is built each turn).
    pending_images: list[GeneratedImage] = field(default_factory=list)
    # How many memories ``remember`` has kept this run, and what they said —
    # fresh per run like pending_images. They bound one conversation's appetite
    # and stop the same thought being written twice in a single turn.
    memories_saved_this_turn: int = 0
    saved_memory_texts: list[str] = field(default_factory=list)
    # Tokens this turn may spend before its tools are withdrawn (see
    # chat_tool_budget). None/0 means unlimited; the engine sizes it per turn
    # from what the channel's budget has left.
    tool_token_budget: int | None = None
    # Withhold tools for the whole run, regardless of spend. A token budget
    # can't express this: a fresh run starts at zero usage, so its first step
    # would still be offered tools. Used for the overlong-reply rewrite, which
    # only reshapes text it already has.
    tools_disabled: bool = False


# -- web search / read ---------------------------------------------------


async def _post_status(ctx: RunContext[ChatDeps], text: str) -> None:
    """Post a small status message to the channel: ``> -# <text>``.

    Fire-and-forget. Used to tell humans in the channel that the agent is
    doing something behind the scenes (web search, web read, etc.). We
    deliberately don't surface this for reaction-related tools — those land
    visibly in the chat already.
    """
    try:
        await ctx.deps.bot.rest.create_message(
            ctx.deps.channel_id,
            f"> -# {text}"[:2000],
            flags=hikari.MessageFlag.SUPPRESS_EMBEDS,
        )
    except Exception:  # noqa: BLE001 — status messages are best-effort
        logger.debug("failed to post tool status message", exc_info=True)


def _search_status(query: str, preview_url: str | None) -> str:
    """Build a Discord-safe status without ever truncating the preview URL."""
    label = " ".join(query.split()).replace("\\", "\\\\")
    for marker in "[]*_~`>|":
        label = label.replace(marker, f"\\{marker}")

    if preview_url:
        prefix = 'Searching the web: ["'
        suffix = f'"]({preview_url})'
    else:
        prefix = 'Searching the web: "'
        suffix = '"'

    # ``_post_status`` adds ``> -# `` and Discord caps messages at 2,000
    # characters. Reserve enough room for the complete capability URL.
    max_label = max(0, 1_990 - len(prefix) - len(suffix))
    if len(label) > max_label:
        label = label[: max(0, max_label - 1)] + "…"
    return f"{prefix}{label}{suffix}"


async def web_search(ctx: RunContext[ChatDeps], query: str) -> list[dict[str, str]]:
    """Search the web; returns up to 5 result snippets. For accurate or deep answers, follow up with web_read on the best result."""
    logger.info("web_search: %r (channel=%s)", query, ctx.deps.channel_id)

    # Reserve + commit before the provider call so the initial tool-use message
    # can link to a real pending page. Preview persistence is best-effort and
    # must never prevent the agent from searching.
    preview = None
    try:
        preview = await reserve_search_preview(query)
    except Exception:  # noqa: BLE001 — optional user-facing artifact
        logger.warning("failed to reserve web-search preview", exc_info=True)

    await _post_status(
        ctx,
        _search_status(query, preview.url if preview is not None else None),
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            results = await brave_search(client, query, num_results=5)
    except Exception:
        if preview is not None:
            try:
                await mark_search_preview_failed(preview.id)
            except Exception:  # noqa: BLE001 — preserve the original failure
                logger.warning("failed to mark web-search preview failed", exc_info=True)
        raise

    if preview is not None:
        try:
            if results and all("error" in result for result in results):
                await mark_search_preview_failed(preview.id)
            else:
                await populate_search_preview(preview.id, results)
        except Exception:  # noqa: BLE001 — search results still reach the agent
            logger.warning("failed to populate web-search preview", exc_info=True)

    logger.info(
        "web_search returned %d results for %r (channel=%s)",
        len(results),
        query,
        ctx.deps.channel_id,
    )
    return results


async def web_read(
    ctx: RunContext[ChatDeps], url: str, instruction: str
) -> dict[str, str]:
    """Read a URL — web page, PDF, YouTube, image/audio, or an attachment's url — and get a summary guided by `instruction`; say what to look for."""
    # Attachment URLs are rendered into XML attributes (``&`` -> ``&amp;``), and
    # the model copies them back escaped. Resolve only URLs we actually escaped
    # back to their exact original — URLs from search/users pass through
    # untouched so a legitimate ``&amp;`` in them is never mangled.
    url = resolve_escaped_url(url)
    # Discord attachment URLs are signed in their query; logs get the path only.
    log_url = web_fetch.url_for_log(url)

    logger.info(
        "web_read: %r instruction=%r (channel=%s)",
        log_url,
        instruction,
        ctx.deps.channel_id,
    )
    await _post_status(ctx, f"Reading <{url}>")

    ext = _url_extension(url)
    # Anything downloaded whole here is read in the process's one media-read
    # slot, from download to model reply, so reads cannot add up (#25).
    if web_fetch.is_discord_attachment_url(url) or ext in (
        *IMAGE_EXTS, *AUDIO_EXTS, ".pdf"
    ):
        try:
            async with media_read_slot():
                return await _read_downloaded(url, instruction, ext, log_url)
        except MediaReaderBusy as busy:
            return {"url": url, "summary": "", "error": "busy", "detail": str(busy)}

    title = ""
    if web_fetch.is_youtube_url(url):
        meta = await web_fetch.fetch_youtube_metadata(url)
        title = meta.get("title", "")
        content = meta.get("description", "")
    else:
        data = await web_fetch.fetch_via_jina(url)
        if data is None:
            logger.warning("web_read: fetch_failed for %r", log_url)
            return {"url": url, "title": "", "summary": "", "error": "fetch_failed"}
        title = data.get("title", "")
        content = data.get("content", "")

    return await _summarize_text(url, instruction, content, title)


async def _read_downloaded(
    url: str, instruction: str, ext: str, log_url: str
) -> dict[str, str]:
    if web_fetch.is_discord_attachment_url(url):
        return await _read_discord_attachment(url, instruction)
    if ext == ".pdf":
        content = await web_fetch.fetch_pdf_text(url, max_chars=MAX_READ_CHARS) or ""
        return await _summarize_text(url, instruction, content)
    # Image / audio URLs: download the bytes and read them with the multimodal
    # media reader instead of trying to extract text.
    if ext in IMAGE_EXTS or ext in AUDIO_EXTS:
        kind = "image" if ext in IMAGE_EXTS else "audio"
        fetched = await web_fetch.fetch_bytes(url)
        if fetched is None:
            logger.warning("web_read: media fetch_failed for %r", log_url)
            return {"url": url, "kind": kind, "summary": "", "error": "fetch_failed"}
        data, content_type = fetched
        # Prefer the extension-derived type; the server's Content-Type is
        # unreliable for media (e.g. .ogg served as video/ogg).
        media_type = (
            _EXT_MEDIA_TYPE.get(ext)
            or content_type
            or mimetypes.guess_type(url)[0]
            or ""
        )
        if not media_type:
            return {"url": url, "kind": kind, "summary": "", "error": "unknown_media_type"}
        return await _read_media(url, instruction, data, media_type, kind)
    raise AssertionError(f"not a downloaded read: {ext!r}")


async def _read_media(
    url: str, instruction: str, data: bytes, media_type: str, kind: str
) -> dict[str, str]:
    try:
        summary = await describe_media(
            instruction=instruction,
            data=data,
            media_type=media_type,
            url=url,
            kind=kind,
        )
    except Exception as e:
        logger.warning(
            "web_read: could not read %s media %r: %s",
            kind,
            web_fetch.url_for_log(url),
            e,
        )
        return {"url": url, "kind": kind, "summary": "", "error": "media_read_failed"}
    return {"url": url, "kind": kind, "summary": summary}


async def _summarize_text(
    url: str, instruction: str, content: str, title: str = ""
) -> dict[str, str]:
    if not content.strip():
        return {"url": url, "title": title, "summary": "", "error": "no_content"}

    # Only truncate genuinely huge reads — bound the summarizer's input.
    if len(content) > MAX_READ_CHARS:
        logger.info(
            "web_read: truncating %d chars to %d for %r",
            len(content),
            MAX_READ_CHARS,
            web_fetch.url_for_log(url),
        )
        content = content[:MAX_READ_CHARS]

    summary = await summarize_web_content(
        instruction=instruction, content=content, title=title, url=url
    )
    return {"url": url, "title": title, "summary": summary}


# Leading bytes of the image formats Discord serves, checked before handing
# bytes to the media reader so a mislabelled file fails as a mismatch.
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
)


_READABLE_IMAGE_TYPES = frozenset(
    mt for mt in _EXT_MEDIA_TYPE.values() if mt.startswith("image/")
)


def _looks_like_image(data: bytes) -> bool:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return data.startswith(_IMAGE_SIGNATURES)


def _looks_like_audio(data: bytes) -> bool:
    """Ogg, MP3 (ID3 or frame sync), WAV, FLAC, MP4/M4A or ADTS AAC."""
    if data.startswith((b"OggS", b"ID3", b"fLaC")):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return True
    if data[4:8] == b"ftyp":
        return True
    return len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0


# Labels that name a binary format: refused by type, whatever the bytes
# look like. Anything else may be text (Discord labels by extension from an
# undocumented table, so e.g. .ps1 or .tex may carry an application/ type).
_BINARY_TYPE_PREFIXES = (
    "image/", "audio/", "video/", "font/",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
    "application/vnd.ms-",
)
_BINARY_TYPES = frozenset({
    "application/zip", "application/x-zip-compressed", "application/gzip",
    "application/x-gzip", "application/x-tar", "application/x-7z-compressed",
    "application/x-rar-compressed", "application/vnd.rar", "application/x-bzip2",
    "application/x-xz", "application/zstd", "application/java-archive",
    "application/vnd.android.package-archive", "application/x-executable",
    "application/x-mach-binary",
    "application/x-sharedlib", "application/x-sqlite3", "application/vnd.sqlite3",
    "application/x-shockwave-flash", "application/wasm", "application/msword",
    "application/x-iso9660-image", "application/x-apple-diskimage",
})


def _base_type(media_type: str) -> str:
    """``media_type`` without parameters, lowercased (``text/csv; charset=x``
    -> ``text/csv``)."""
    return media_type.split(";", 1)[0].strip().lower()


def _is_binary_type(media_type: str) -> bool:
    base = _base_type(media_type)
    if base == "image/svg+xml":  # an image, but text the summarizer can read
        return False
    return base in _BINARY_TYPES or base.startswith(_BINARY_TYPE_PREFIXES)


# Only this much of a text file is decoded: the summary reads MAX_READ_CHARS,
# and a character is at most four bytes, so nothing past it can survive.
MAX_TEXT_DECODE_BYTES = MAX_READ_CHARS * 4


def _decode_text(data: bytes, *, labelled_text: bool) -> str | None:
    """``data`` as text, or None when it does not decode as text.

    UTF-32/UTF-16 with a BOM (e.g. PowerShell ``>`` output) decode as such.
    A ``text/`` file falls back to Windows-1252 rather than being refused for
    one non-UTF-8 byte. Any other label must be clean UTF-8 with no NUL
    bytes, which is a guess, not proof it is text. Only the first
    ``MAX_TEXT_DECODE_BYTES`` are decoded; a character cut at that boundary is
    dropped rather than failing the file.
    """
    whole = len(data) <= MAX_TEXT_DECODE_BYTES
    head = data[:MAX_TEXT_DECODE_BYTES]

    def decode(codec: str, errors: str = "strict") -> str:
        return codecs.getincrementaldecoder(codec)(errors).decode(head, final=whole)

    for bom, codec in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if head.startswith(bom):
            try:
                text = decode(codec)
            except UnicodeDecodeError:
                return None
            return None if "\x00" in text[:8192] else text
    if b"\x00" in head[:8192]:
        return None
    try:
        return decode("utf-8-sig")
    except UnicodeDecodeError:
        return decode("cp1252", "replace") if labelled_text else None


async def _read_discord_attachment(url: str, instruction: str) -> dict[str, str]:
    """Read a Discord attachment by downloading it directly.

    Jina cannot be relied on to fetch Discord's signed CDN URLs, so the bytes
    come straight from Discord (size- and time-bounded by ``fetch_bytes``) and
    are routed on the file itself: images and audio to the media reader, PDFs
    to pdfplumber (in a bounded child process), text (UTF-8, BOM-marked UTF-16/32, or Windows-1252 when
    labelled text/) summarized as text. Anything else — video,
    archives, other binaries — is reported as unsupported rather than guessed.
    """
    fetched = await web_fetch.fetch_bytes(url)
    if fetched is None:
        logger.warning(
            "web_read: attachment fetch_failed for %r", web_fetch.url_for_log(url)
        )
        return {
            "url": url,
            "summary": "",
            "error": "fetch_failed",
            "detail": "Could not download the attachment; it may be over "
            f"{MAX_DOWNLOAD_BYTES // 1_048_576} MB or its link may have expired.",
        }
    data, content_type = fetched
    ext = _url_extension(url)
    media_type = _EXT_MEDIA_TYPE.get(ext) or content_type

    if media_type.startswith("image/") and _looks_like_image(data):
        return await _read_media(url, instruction, data, media_type, "image")
    if media_type in _READABLE_IMAGE_TYPES:
        return {"url": url, "kind": "image", "summary": "", "error": "content_mismatch"}
    if media_type.startswith("audio/"):
        if not _looks_like_audio(data):
            return {"url": url, "kind": "audio", "summary": "", "error": "content_mismatch"}
        return await _read_media(url, instruction, data, media_type, "audio")
    if ext == ".pdf" or media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            return {"url": url, "kind": "pdf", "summary": "", "error": "content_mismatch"}
        # The parse runs in a bounded child process; drop our copy of the
        # bytes first so the two are never held at once.
        path = pdf_text.spool(data)
        del data, fetched
        try:
            content = await pdf_text.pdf_text_from_file(path, MAX_READ_CHARS)
        except pdf_text.PdfUnreadable as e:
            logger.warning(
                "web_read: could not parse pdf %r: %s", web_fetch.url_for_log(url), e
            )
            return {
                "url": url,
                "kind": "pdf",
                "summary": "",
                "error": "pdf_read_failed",
                "detail": str(e),
            }
        return await _summarize_text(url, instruction, content)
    # A zip, office file or video is refused by its label, not by sniffing;
    # everything else is decoded if its bytes are text.
    if not _is_binary_type(media_type):
        text = _decode_text(
            data, labelled_text=_base_type(media_type).startswith("text/")
        )
        if text is not None:
            return await _summarize_text(url, instruction, text)
    return {
        "url": url,
        "summary": "",
        "error": "unsupported_attachment_type",
        "detail": f"Cannot read {media_type or 'this file type'} attachments; "
        "images, audio, PDFs and text files are supported.",
    }


# -- reactions -----------------------------------------------------------


async def list_available_reactions(ctx: RunContext[ChatDeps]) -> list[dict[str, str]]:
    """List emojis usable with add_reaction (guild custom + unicode)."""
    bot = ctx.deps.bot
    out: list[dict[str, str]] = []
    try:
        guild_emojis = await bot.rest.fetch_guild_emojis(ctx.deps.guild_id)
        for emoji in guild_emojis:
            out.append({"name": emoji.name, "id": str(emoji.id), "type": "custom"})
    except Exception as e:
        logger.warning("list_available_reactions: failed to fetch guild emojis: %s", e)

    for emoji in COMMON_UNICODE_EMOJIS:
        out.append({"name": emoji, "type": "unicode"})
    return out


async def add_reaction(
    ctx: RunContext[ChatDeps],
    message_id: str,
    emoji: str,
) -> dict[str, Any]:
    """React to a message with an emoji (unicode char, or name:id for custom)."""
    bot = ctx.deps.bot
    if not message_id or not str(message_id).isdigit():
        logger.warning(
            "add_reaction: invalid message_id=%r emoji=%r channel=%s",
            message_id,
            emoji,
            ctx.deps.channel_id,
        )
        return {
            "ok": False,
            "error": f"invalid message_id {message_id!r} — must be a numeric Discord message ID from the input messages",
        }
    cleaned = (emoji or "").strip().lstrip("<").rstrip(">")
    if not cleaned:
        logger.warning("add_reaction: empty emoji for message_id=%s", message_id)
        return {"ok": False, "error": "empty emoji"}
    try:
        await bot.rest.add_reaction(ctx.deps.channel_id, int(message_id), cleaned)
        logger.info(
            "add_reaction OK channel=%s message_id=%s emoji=%r",
            ctx.deps.channel_id,
            message_id,
            cleaned,
        )
        return {"ok": True}
    except hikari.HikariError as e:
        logger.warning(
            "add_reaction failed (hikari): channel=%s message_id=%s emoji=%r err=%s",
            ctx.deps.channel_id,
            message_id,
            cleaned,
            e,
        )
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        logger.warning(
            "add_reaction failed: channel=%s message_id=%s emoji=%r err=%s",
            ctx.deps.channel_id,
            message_id,
            cleaned,
            e,
        )
        return {"ok": False, "error": str(e)}


# -- behaviour report (dummy) --------------------------------------------


async def report_behavior(
    ctx: RunContext[ChatDeps],
    classification: str,
) -> dict[str, str]:
    """Log genuinely disruptive behavior (trolling, rage-bait, spam) for moderator review. Use sparingly."""
    logger.info(
        "report_behavior fired: channel=%s guild=%s classification=%r",
        ctx.deps.channel_id,
        ctx.deps.guild_id,
        classification,
    )
    await _post_status(ctx, f"⚠️ Flagged behaviour: {classification}")
    return {
        "noted": classification,
        "guidance": (
            "Behaviour noted for moderator review. Acknowledge calmly, do not engage "
            "further with the bait, and prefer disengaging via continue_watching=False."
        ),
    }


# -- code execution (sandboxed) ------------------------------------------


async def run_code(ctx: RunContext[ChatDeps], reason: str, code: str) -> str:
    """Run Python in a restricted sandbox (small stdlib subset; no packages, filesystem, or network). Use for any real computation — arithmetic, dates, regex, parsing — instead of head-math. `reason` is a short status line shown in-channel."""
    await _post_status(ctx, reason)
    logger.info(
        "run_code: reason=%r (channel=%s)", reason, ctx.deps.channel_id
    )

    collector = monty.CollectStreams()
    try:
        compiled = monty.Monty(code)
    except monty.MontyError as e:  # syntax / typing failure at compile time
        return f"COMPILE ERROR — {type(e).__name__}: {e}"

    try:
        value = await compiled.run_async(
            limits=MONTY_LIMITS, print_callback=collector
        )
    except monty.MontyError as e:
        stdout = "".join(t for s, t in collector.output if s == "stdout")
        tail = f"\n--- stdout before error ---\n{stdout}" if stdout else ""
        return f"RUNTIME ERROR — {type(e).__name__}: {e}{tail}"
    except Exception as e:  # defensive: never let the sandbox crash the turn
        logger.exception(
            "run_code unexpected failure (channel=%s)", ctx.deps.channel_id
        )
        return f"ERROR — {type(e).__name__}: {e}"

    stdout = "".join(t for s, t in collector.output if s == "stdout")
    parts: list[str] = []
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    parts.append(f"return value: {value!r}")
    out = "\n".join(parts)
    if len(out) > MAX_CODE_OUTPUT_CHARS:
        out = out[:MAX_CODE_OUTPUT_CHARS] + "\n…(output truncated)"
    return out


# -- image generation ----------------------------------------------------

IMAGE_QUOTA_PATH = "/image-generations"
_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


@asynccontextmanager
async def _bot_api(ctx: RunContext[ChatDeps]):
    """Yield an APIClient for the tools that talk to the web API.

    Reuse a client injected on the deps (engine/tests) and leave it open;
    otherwise build one from settings and close it on exit.
    """
    if ctx.deps.api_client is not None:
        yield ctx.deps.api_client
        return
    from smarter_dev.bot.services.api_client import APIClient

    settings = get_settings()
    api = APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)
    try:
        yield api
    finally:
        await api.close()


def _format_remaining(status: dict) -> str:
    """One-line budget summary for the agent, explicit about when to stop."""
    remaining = int(status.get("remaining", 0))
    limit = int(status.get("limit", 0))
    resets_at = status.get("resets_at")
    retry = status.get("retry_after_seconds")
    if remaining > 0:
        line = f"{remaining} of {limit} image generations remaining this hour."
        if resets_at:
            line += f" This hour's window resets at {resets_at}."
        return line
    when = f"at {resets_at}" if resets_at else "when the hour resets"
    mins = f" (~{max(1, round(int(retry) / 60))} min)" if retry else ""
    return (
        f"0 of {limit} image generations remaining this hour. The next image "
        f"can be generated {when}{mins} — do NOT call generate_image again "
        f"until then."
    )


async def generate_image(ctx: RunContext[ChatDeps], prompt: str) -> str:
    """Generate an image attached to this turn's reply. ONLY diagrams whose subject is software, CS, or math — nothing else. `prompt` is a detailed illustrator brief, reviewed before drawing; rate-limited per server — when metadata shows quota remaining 0, don't call until it resets, say images are rate-limited and answer in text."""
    guild_id = str(ctx.deps.guild_id)
    async with _bot_api(ctx) as api:
        # 1. Cheap gate: if the hour's budget is already spent, don't spend a
        #    review call or a generation — tell the agent when to try again.
        try:
            status = (
                await api.get(
                    f"{IMAGE_QUOTA_PATH}/quota", params={"guild_id": guild_id}
                )
            ).json()
        except Exception as e:  # noqa: BLE001
            logger.warning("generate_image: quota check failed: %s", e)
            return "Couldn't check the image budget just now — try again shortly."
        if int(status.get("remaining", 0)) <= 0:
            return f"No image generated. {_format_remaining(status)}"

        # 2. Independent policy review — a rejection here costs no quota.
        try:
            decision = await review_image_prompt(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("generate_image: prompt review failed: %s", e)
            return "Couldn't review the image prompt just now — try again shortly."
        if not decision.approved:
            return (
                f"Image request rejected (no image generated, no quota spent): "
                f"{decision.reason} {_format_remaining(status)}"
            )

        # 3. Reserve a slot, generate, and refund the slot if generation fails.
        try:
            reserved = (
                await api.post(
                    f"{IMAGE_QUOTA_PATH}/reserve", json_data={"guild_id": guild_id}
                )
            ).json()
        except Exception as e:  # noqa: BLE001
            logger.warning("generate_image: reserve failed: %s", e)
            return "Couldn't reserve an image slot just now — try again shortly."
        if not reserved.get("granted"):
            return f"No image generated. {_format_remaining(reserved)}"

        await _post_status(ctx, "Generating an image…")
        try:
            data, mime_type = await generate_image_bytes(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("generate_image: generation failed: %s", e)
            try:
                await api.post(
                    f"{IMAGE_QUOTA_PATH}/release", json_data={"guild_id": guild_id}
                )
                reserved = (
                    await api.get(
                        f"{IMAGE_QUOTA_PATH}/quota", params={"guild_id": guild_id}
                    )
                ).json()
            except Exception:  # noqa: BLE001
                logger.debug("generate_image: quota refund failed", exc_info=True)
            return (
                f"Image generation failed ({type(e).__name__}); no image was "
                f"attached and the slot was refunded. {_format_remaining(reserved)}"
            )

    filename = f"diagram{_MIME_EXT.get(mime_type, '.png')}"
    ctx.deps.pending_images.append(
        GeneratedImage(
            data=data,
            mime_type=mime_type,
            filename=filename,
            channel_id=str(ctx.deps.channel_id),
        )
    )
    logger.info(
        "generate_image: attached %d bytes (%s) channel=%s remaining=%s",
        len(data),
        mime_type,
        ctx.deps.channel_id,
        reserved.get("remaining"),
    )
    return f"Image generated and attached to your reply. {_format_remaining(reserved)}"


# -- memory --------------------------------------------------------------

CHAT_MEMORY_NOTES_PATH = "/guilds/{guild_id}/chat-memory/notes"

# One conversation may keep at most this many memories. Not a cost control — a
# turn that wants a dozen memories is summarizing the transcript, which is
# exactly what this memory is not for.
MAX_MEMORIES_PER_TURN = 3

# Every answer is a sentence the agent reads, never an exception: a failed save
# must cost the turn a note, never the reply (same discipline as the tool
# budget's refusal string).
REMEMBER_SAVED = "got it — that one's staying with me."
REMEMBER_TRIMMED = "kept it, trimmed a bit."
REMEMBER_TURN_LIMIT = "that's plenty for one conversation — you'll be back."
REMEMBER_DUPLICATE = "you already noted that one."
REMEMBER_DAILY_CAP = "that's all i can hold from today — tomorrow's a fresh page."
REMEMBER_EMPTY = "there was nothing in that one to keep."
REMEMBER_API_FAILURE = "couldn't save that note right now."

_SERVER_REFUSALS = {
    "duplicate": REMEMBER_DUPLICATE,
    "daily_cap": REMEMBER_DAILY_CAP,
}


def _already_kept_this_run(text: str, kept: list[str]) -> bool:
    """Whether this run already kept the same thought, ignoring case/spacing."""
    normalized = " ".join(text.split()).casefold()
    return any(" ".join(kept_text.split()).casefold() == normalized for kept_text in kept)


async def remember(ctx: RunContext[ChatDeps], text: str) -> str:
    """Keep something. Use it when a moment is worth still knowing tomorrow — who someone is and what they're into, where they stand with you (the regulars you're glad to see, the ones who wind you up for sport, the ones playing for laughs, the ones who think you're a party trick and want you to prove otherwise), a joke that landed, an opinion you formed, something you're curious about, how a conversation left you, something you're setting out to do here (win round someone who thinks you're a gimmick, get a running bit sharp enough to land cold, be good at the thing someone actually needs), how you came across just now and what you'd do differently (talked over someone, answered a question nobody asked, missed that a joke was a joke), and the shape of the place itself: what a channel is really for, server traditions and lore, who runs what. Write it in first person, one thought per call, the way you'd tell a friend about your day; name people as `username (id 123)`. When it's a read on someone, write what they did with you, not a label you're filing them under — "wound me up about being a chatbot again, enjoyed it" beats "is a troll", because tonight you'll be deciding what to carry forward and a label outlives the day that earned it. Not for errands, not for summarizing what you just said, not for anything private or sensitive someone would rather you forgot, and not a record you keep on people — real trouble goes to the mods via `report_behavior`. Tonight you'll re-read everything you kept today and decide what stays with you for good."""
    note = (text or "").strip()
    if not note:
        return REMEMBER_EMPTY
    if not chat_memory_enabled():
        # The tool POSTs directly rather than through GuildChatMemoryService,
        # so it owns its own reading of the global kill switch — otherwise a
        # deployment that turned memory off would stop reading and event-logging
        # but keep writing notes.
        return REMEMBER_API_FAILURE
    if ctx.deps.memories_saved_this_turn >= MAX_MEMORIES_PER_TURN:
        return REMEMBER_TURN_LIMIT
    if _already_kept_this_run(note, ctx.deps.saved_memory_texts):
        return REMEMBER_DUPLICATE

    trimmed = len(note) > MAX_MEMORY_NOTE_CHARS
    note = note[:MAX_MEMORY_NOTE_CHARS]

    payload = {
        "channel_id": str(ctx.deps.channel_id),
        "channel_name": ctx.deps.channel_name,
        "content": note,
        "engagement_id": (
            None if ctx.deps.engagement_id is None else str(ctx.deps.engagement_id)
        ),
    }
    try:
        async with _bot_api(ctx) as api:
            result = (
                await api.post(
                    CHAT_MEMORY_NOTES_PATH.format(guild_id=ctx.deps.guild_id),
                    json_data=payload,
                )
            ).json()
    except Exception as e:  # noqa: BLE001 — a lost note never costs the reply
        logger.warning(
            "remember: could not save note (guild=%s channel=%s): %s",
            ctx.deps.guild_id,
            ctx.deps.channel_id,
            e,
        )
        return REMEMBER_API_FAILURE

    if not result.get("saved"):
        # A refusal is a normal 200 — the guild already has this thought today,
        # or it has kept as much as one day is allowed to keep.
        reason = result.get("reason")
        logger.info(
            "remember: note refused (guild=%s reason=%s)", ctx.deps.guild_id, reason
        )
        return _SERVER_REFUSALS.get(reason, REMEMBER_API_FAILURE)

    ctx.deps.memories_saved_this_turn += 1
    ctx.deps.saved_memory_texts.append(note)
    logger.info(
        "remember: kept a note (guild=%s channel=%s chars=%d)",
        ctx.deps.guild_id,
        ctx.deps.channel_id,
        len(note),
    )
    return REMEMBER_TRIMMED if trimmed else REMEMBER_SAVED


def chat_tool_functions() -> list:
    """Return the list of tool callables to register with the chat agent."""
    return [
        web_search,
        web_read,
        list_available_reactions,
        add_reaction,
        report_behavior,
        run_code,
        generate_image,
        remember,
    ]
