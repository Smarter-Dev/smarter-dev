"""Tool implementations for the chat agent.

These are bound to a per-turn context (bot + channel + guild) via
``create_chat_tools()``. The resulting list is registered with the
Pydantic AI Agent as its tool surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import hikari
import httpx
from pydantic_ai import RunContext

from smarter_dev.bot.utils import web_fetch
from smarter_dev.web.scan.tools import brave_search

logger = logging.getLogger(__name__)

COMMON_UNICODE_EMOJIS = [
    "👍", "👎", "❤️", "😀", "😂", "🤔", "🎉", "🔥", "✨",
    "😍", "🙏", "👀", "💯", "🤷", "🚀", "✅", "❌",
]


@dataclass
class ChatDeps:
    """Per-run dependencies injected into chat agent tool calls."""

    bot: Any  # hikari.GatewayBot — typed as Any so tests can pass a mock
    channel_id: int
    guild_id: int


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


async def web_read(ctx: RunContext[ChatDeps], url: str) -> dict[str, str]:
    """Fetch a URL and return its readable text content.

    Returns a dict with ``title``, ``description``, ``content``, and ``url``.
    PDF URLs are extracted via pdfplumber; YouTube URLs return video metadata.
    """
    logger.info("web_read: %r (channel=%s)", url, ctx.deps.channel_id)
    await _post_status(ctx, f"Reading <{url}>")

    if web_fetch.is_youtube_url(url):
        meta = await web_fetch.fetch_youtube_metadata(url)
        return {
            "url": url,
            "title": meta.get("title", ""),
            "description": meta.get("description", ""),
            "content": meta.get("description", ""),
            "author": meta.get("author", ""),
        }

    if url.lower().endswith(".pdf"):
        text = await web_fetch.fetch_pdf_text(url)
        return {
            "url": url,
            "title": "",
            "description": "",
            "content": text or "",
        }

    data = await web_fetch.fetch_via_jina(url)
    if data is None:
        logger.warning("web_read: fetch_failed for %r", url)
        return {"url": url, "title": "", "description": "", "content": "", "error": "fetch_failed"}
    return data


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


def chat_tool_functions() -> list:
    """Return the list of tool callables to register with the chat agent."""
    return [
        web_search,
        web_read,
        list_available_reactions,
        add_reaction,
        report_behavior,
    ]
