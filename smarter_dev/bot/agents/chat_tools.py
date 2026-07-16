"""Tool implementations for the chat agent.

These are bound to a per-turn context (bot + channel + guild) via
``create_chat_tools()``. The resulting list is registered with the
Pydantic AI Agent as its tool surface.
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from smarter_dev.shared.config import get_settings
from smarter_dev.web.research_tools import brave_search

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


@dataclass
class ChatDeps:
    """Per-run dependencies injected into chat agent tool calls."""

    bot: Any  # hikari.GatewayBot — typed as Any so tests can pass a mock
    channel_id: int
    guild_id: int
    # APIClient for the handler-management + image-quota tools; built from
    # settings on demand when not supplied (see handler_tools / _quota_api).
    api_client: Any = None
    # Images generated this turn, drained by the engine and attached to the
    # outgoing reply. Fresh per run (a new ChatDeps is built each turn).
    pending_images: list[GeneratedImage] = field(default_factory=list)


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
            ctx.deps.channel_id, f"> -# {text}"[:2000]
        )
    except Exception:  # noqa: BLE001 — status messages are best-effort
        logger.debug("failed to post tool status message", exc_info=True)


async def web_search(ctx: RunContext[ChatDeps], query: str) -> list[dict[str, str]]:
    """Search the web via Brave Search and return up to 5 result snippets.

    Use this when the user asks about current events, specific products,
    niche technical topics, or anything that needs sources to back it up.
    """
    logger.info("web_search: %r (channel=%s)", query, ctx.deps.channel_id)
    await _post_status(ctx, f'Searching the web: "{query}"')
    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await brave_search(client, query, num_results=5)
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
    """Fetch a URL and return an instruction-guided summary of its content.

    Handles web pages, PDFs, YouTube links, and image/audio files (e.g.
    ``<attachment>`` URLs). A fast model condenses the content per
    ``instruction`` — you get the summary back, never the raw file — so say
    what to look for, what to ignore, quote-vs-paraphrase, and how much
    detail (keep it small; ~5 paragraphs max). If the page lacks what you
    asked for, the summary says so instead of guessing. Returns ``url``,
    ``title``, and ``summary`` (or ``error`` on fetch failure).
    """
    # Attachment URLs are rendered into XML attributes (``&`` -> ``&amp;``), and
    # the model copies them back escaped. Resolve only URLs we actually escaped
    # back to their exact original — URLs from search/users pass through
    # untouched so a legitimate ``&amp;`` in them is never mangled.
    url = resolve_escaped_url(url)

    logger.info(
        "web_read: %r instruction=%r (channel=%s)",
        url,
        instruction,
        ctx.deps.channel_id,
    )
    await _post_status(ctx, f"Reading <{url}>")

    # Image / audio URLs: download the bytes and read them with the multimodal
    # media reader instead of trying to extract text.
    ext = _url_extension(url)
    if ext in IMAGE_EXTS or ext in AUDIO_EXTS:
        kind = "image" if ext in IMAGE_EXTS else "audio"
        fetched = await web_fetch.fetch_bytes(url)
        if fetched is None:
            logger.warning("web_read: media fetch_failed for %r", url)
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
        try:
            summary = await describe_media(
                instruction=instruction,
                data=data,
                media_type=media_type,
                url=url,
                kind=kind,
            )
        except Exception as e:
            logger.warning("web_read: could not read %s media %r: %s", kind, url, e)
            return {"url": url, "kind": kind, "summary": "", "error": "media_read_failed"}
        return {"url": url, "kind": kind, "summary": summary}

    title = ""
    if web_fetch.is_youtube_url(url):
        meta = await web_fetch.fetch_youtube_metadata(url)
        title = meta.get("title", "")
        content = meta.get("description", "")
    elif url.lower().endswith(".pdf"):
        content = await web_fetch.fetch_pdf_text(url, max_chars=MAX_READ_CHARS) or ""
    else:
        data = await web_fetch.fetch_via_jina(url)
        if data is None:
            logger.warning("web_read: fetch_failed for %r", url)
            return {"url": url, "title": "", "summary": "", "error": "fetch_failed"}
        title = data.get("title", "")
        content = data.get("content", "")

    if not content.strip():
        return {"url": url, "title": title, "summary": "", "error": "no_content"}

    # Only truncate genuinely huge reads — bound the summarizer's input.
    if len(content) > MAX_READ_CHARS:
        logger.info(
            "web_read: truncating %d chars to %d for %r",
            len(content),
            MAX_READ_CHARS,
            url,
        )
        content = content[:MAX_READ_CHARS]

    summary = await summarize_web_content(
        instruction=instruction, content=content, title=title, url=url
    )
    return {"url": url, "title": title, "summary": summary}


# -- reactions -----------------------------------------------------------


async def list_available_reactions(ctx: RunContext[ChatDeps]) -> list[dict[str, str]]:
    """List emojis the bot can use with ``add_reaction``: guild custom + unicode."""
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
    """Add an emoji reaction to a specific message in the current channel.

    For custom emojis, pass ``name:id`` (e.g. ``thinking:123456789``).
    For unicode, pass the emoji character directly.
    """
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
    """Report problematic user behaviour.

    Use sparingly — only when a user is genuinely trying to provoke, rage-bait,
    or repeatedly disrupt. The call is logged for moderator review.

    Args:
        classification: short label, e.g. "rage_bait", "spam", "trolling".

    Returns:
        A guidance string the agent should reflect in its response (e.g. acknowledge
        the behaviour was noted) — the agent may then choose to disengage.
    """
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
    """Run Python in a secure sandbox (Pydantic Monty) and return its output.

    Use for any real computation (arithmetic, date math, regex, parsing,
    counting) instead of head-math. Returns stdout plus the final
    expression's value (like a notebook cell), or the error. ``reason`` is a
    short human status line (5-10 words) shown in-channel, e.g.
    "Calculating the 30-day total". RESTRICTED Python subset: allowed
    stdlib only (sys, os, typing, asyncio, re, datetime, json), no
    third-party packages, no ``class``/``match``, no filesystem/network/env
    access, a few seconds of runtime — pure computation only.
    """
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
async def _quota_api(ctx: RunContext[ChatDeps]):
    """Yield an APIClient for the image-quota endpoints.

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
    """Generate an image and attach it to your reply this turn.

    STRICT policy: only diagrams whose SUBJECT is SOFTWARE, CS, or MATH
    (data structures, algorithms, architecture/protocol diagrams, state
    machines, DB schemas, UML, math figures, complexity curves; charts only
    when plotting code/CS/math data). NEVER other-science diagrams,
    non-technical charts, politics, off-topic subjects, art/memes/logos, or
    real people. ``prompt`` is a detailed illustrator brief, reviewed
    before generation — a rejection returns an explanation, spends no
    quota, and must not be retried verbatim. Rate-limited per server; the
    return value says how many images remain (once it says 0, don't call
    again until the stated time). On success the image attaches to this
    turn's message — introduce it in your reply.
    """
    guild_id = str(ctx.deps.guild_id)
    async with _quota_api(ctx) as api:
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
        GeneratedImage(data=data, mime_type=mime_type, filename=filename)
    )
    logger.info(
        "generate_image: attached %d bytes (%s) channel=%s remaining=%s",
        len(data),
        mime_type,
        ctx.deps.channel_id,
        reserved.get("remaining"),
    )
    return f"Image generated and attached to your reply. {_format_remaining(reserved)}"


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
    ]
