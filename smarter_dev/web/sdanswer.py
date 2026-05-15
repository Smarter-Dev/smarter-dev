"""Parse and enrich ``sdanswer`` fenced blocks in agent answers.

The Resource Agent can emit JSON inside ```` ```sdanswer ```` fences to render
richer UI than plain markdown — article/snippet/collection card rows and
ordered "resource paths". This module:

1. Renders the agent's markdown via :func:`render_markdown`.
2. Walks the resulting HTML for ``<pre><code class="language-sdanswer">…</code></pre>``
   blocks, parses the JSON, looks up referenced URLs in ``resource_sources``
   to enrich titles/blurbs/etc., and replaces each fence with an inert
   ``<div class="sdanswer" data-block-id="N">`` placeholder. The frontend
   reads the matching JSON payload from a sibling ``<script type="application/json">``
   and hydrates the DOM.
3. Returns the enriched HTML plus the ordered list of resolved block payloads.

Failure modes (malformed JSON, unknown ``type``, all URLs missing) degrade
to a normal code block — we never break the page on a bad block.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.lib.markdown import render_markdown

from smarter_dev.web.models import ResourceSource

logger = logging.getLogger(__name__)


_VALID_DIRECTORY_SLUGS = {
    "agentic-coding-courses",
    "system-architecture",
    "infrastructure-hosting",
    "software-delivery",
    "production-operations",
}

_FENCE_RE = re.compile(
    r'<pre><code class="language-sdanswer">(.*?)</code></pre>',
    re.DOTALL,
)

# Card kinds that the agent sometimes emits at the top level without wrapping
# them in a `{"type": "cards", "cards": [...]}` envelope. We accept those and
# wrap them ourselves so a missing wrapper doesn't dump raw JSON on the page.
_BARE_CARD_KINDS = {"article", "snippet", "collection", "tradeoff", "prereq", "gotcha"}


async def enrich_answer(
    db_session: AsyncSession, markdown_content: str
) -> tuple[str, list[dict]]:
    """Render markdown + enrich any ``sdanswer`` fences.

    Returns ``(html, blocks)``. ``blocks`` is the ordered list of resolved
    payloads (one per surviving fence); the matching placeholder in HTML
    carries ``data-block-id="<index>"``.
    """
    html = render_markdown(markdown_content or "")
    if "language-sdanswer" not in html:
        return html, []

    # Collect all referenced URLs in one pass so the DB lookup is a single
    # query regardless of how many blocks the answer contains.
    raw_blocks: list[dict] = []
    matches = list(_FENCE_RE.finditer(html))
    for match in matches:
        raw_blocks.append(_decode_block(match.group(1)))

    needed_urls: set[str] = set()
    for block in raw_blocks:
        if not block.get("_ok"):
            continue
        payload = block["payload"]
        if payload.get("type") == "cards":
            for card in payload.get("cards") or []:
                if card.get("type") == "article" and card.get("url"):
                    needed_urls.add(card["url"])
                elif card.get("type") == "collection":
                    for link in card.get("links") or []:
                        if isinstance(link, str):
                            needed_urls.add(link)
                elif card.get("type") == "prereq":
                    for item in card.get("items") or []:
                        if isinstance(item, dict) and isinstance(item.get("url"), str):
                            needed_urls.add(item["url"])
        elif payload.get("type") == "path":
            for link in payload.get("links") or []:
                url = _coerce_path_url(link)
                if url:
                    needed_urls.add(url)

    sources_by_url = await _load_sources(db_session, needed_urls)

    resolved_blocks: list[dict] = []
    parts: list[str] = []
    cursor = 0
    for raw, match in zip(raw_blocks, matches):
        parts.append(html[cursor : match.start()])
        cursor = match.end()

        block = _resolve_block(raw, sources_by_url)
        if block is None:
            # Bad block (invalid JSON, unknown top-level type, or every
            # referenced URL missed the catalog). Drop the fence entirely
            # rather than dump raw JSON in front of the reader — the
            # logger.warning in `_resolve_block` is enough to debug from.
            continue

        idx = len(resolved_blocks)
        resolved_blocks.append(block)
        parts.append(
            f'<div class="sdanswer" data-block-id="{idx}" data-block-type="{block["type"]}"></div>'
        )

    parts.append(html[cursor:])
    return "".join(parts), resolved_blocks


def _decode_block(inner_html: str) -> dict:
    """Decode the JSON inside a fence. Always returns a dict with ``_ok``."""
    raw = _unescape_inner_html(inner_html).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("sdanswer block: invalid JSON (%s)", exc)
        return {"_ok": False}
    if not isinstance(payload, dict):
        logger.warning("sdanswer block: top-level must be an object")
        return {"_ok": False}
    if payload.get("type") in _BARE_CARD_KINDS:
        payload = {"type": "cards", "cards": [payload]}
    return {"_ok": True, "payload": payload}


def _unescape_inner_html(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&#34;", '"')
    )


async def _load_sources(
    db_session: AsyncSession, urls: set[str]
) -> dict[str, ResourceSource]:
    if not urls:
        return {}
    result = await db_session.execute(
        select(ResourceSource).where(ResourceSource.url.in_(urls))
    )
    return {src.url: src for src in result.scalars().all()}


def _resolve_block(
    raw: dict, sources_by_url: dict[str, ResourceSource]
) -> dict | None:
    if not raw.get("_ok"):
        return None
    payload = raw["payload"]
    block_type = payload.get("type")

    if block_type == "cards":
        cards = _resolve_cards(payload.get("cards") or [], sources_by_url)
        if not cards:
            return None
        return {"type": "cards", "cards": cards}

    if block_type == "path":
        steps = _resolve_path_steps(payload.get("links") or [], sources_by_url)
        if not steps:
            return None
        # Path total is aggregated from per-step estimates only — Gemini is
        # bad at estimating the whole trip but ok at the individual rungs.
        total_minutes = sum(step.pop("estimate_minutes", 0) for step in steps)
        return {
            "type": "path",
            "title": _trim_str(payload.get("title"), 120),
            "estimate": _format_minutes(total_minutes) if total_minutes else None,
            "steps": steps,
        }

    logger.warning("sdanswer block: unknown type %r", block_type)
    return None


def _resolve_cards(
    cards: list[Any], sources_by_url: dict[str, ResourceSource]
) -> list[dict]:
    out: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        kind = card.get("type")
        if kind == "article":
            url = card.get("url")
            src = sources_by_url.get(url) if isinstance(url, str) else None
            if src is None:
                continue
            out.append(_source_to_article_card(src))
        elif kind == "snippet":
            resolved = _resolve_snippet(card)
            if resolved is not None:
                out.append(resolved)
        elif kind == "collection":
            resolved = _resolve_collection(card, sources_by_url)
            if resolved is not None:
                out.append(resolved)
        elif kind == "tradeoff":
            resolved = _resolve_tradeoff(card)
            if resolved is not None:
                out.append(resolved)
        elif kind == "prereq":
            resolved = _resolve_prereq(card, sources_by_url)
            if resolved is not None:
                out.append(resolved)
        elif kind == "gotcha":
            resolved = _resolve_gotcha(card)
            if resolved is not None:
                out.append(resolved)
        if len(out) >= 4:
            break
    return out


def _source_to_article_card(src: ResourceSource) -> dict:
    return {
        "type": "article",
        "url": src.url,
        "title": src.title,
        "byline": src.byline or "",
        "blurb": src.blurb or "",
        "learning_type": src.learning_type,
        "track_key": src.track_key,
    }


def _resolve_snippet(card: dict) -> dict | None:
    title = _trim_str(card.get("title"), 200)
    snippet = card.get("snippet")
    if not title or not isinstance(snippet, str) or not snippet.strip():
        return None
    return {
        "type": "snippet",
        "title": title,
        "description": _trim_str(card.get("description"), 400) or "",
        "snippet": snippet,
        "language": _trim_str(card.get("language"), 32) or "",
        "category": _validate_category(card.get("category")),
    }


def _resolve_collection(
    card: dict, sources_by_url: dict[str, ResourceSource]
) -> dict | None:
    title = _trim_str(card.get("title"), 200)
    links = card.get("links") or []
    if not title or not isinstance(links, list):
        return None
    items: list[dict] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, str) or link in seen:
            continue
        seen.add(link)
        src = sources_by_url.get(link)
        if src is None:
            continue
        items.append(_source_to_article_card(src))
    if not items:
        return None
    return {
        "type": "collection",
        "title": title,
        "description": _trim_str(card.get("description"), 400) or "",
        "category": _validate_category(card.get("category")),
        "items": items,
    }


def _resolve_tradeoff(card: dict) -> dict | None:
    title = _trim_str(card.get("title"), 200)
    raw_options = card.get("options") or []
    if not title or not isinstance(raw_options, list):
        return None
    options: list[dict] = []
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        label = _trim_str(opt.get("label"), 120)
        if not label:
            continue
        bullets_raw = opt.get("bullets") or []
        if not isinstance(bullets_raw, list):
            continue
        bullets: list[str] = []
        for b in bullets_raw:
            cleaned = _trim_str(b, 200)
            if cleaned:
                bullets.append(cleaned)
            if len(bullets) >= 6:
                break
        if not bullets:
            continue
        options.append({"label": label, "bullets": bullets})
        if len(options) >= 3:
            break
    if len(options) < 2:
        return None
    return {
        "type": "tradeoff",
        "title": title,
        "options": options,
    }


def _resolve_prereq(
    card: dict, sources_by_url: dict[str, ResourceSource]
) -> dict | None:
    raw_items = card.get("items") or []
    if not isinstance(raw_items, list):
        return None
    items: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        label = _trim_str(item.get("label"), 160)
        if not label:
            continue
        url_raw = item.get("url")
        url = _trim_str(url_raw, 500) if isinstance(url_raw, str) else None
        # If the agent supplied a URL, it must resolve to a curated source —
        # otherwise we drop the URL and keep the label as plain text.
        if url and url not in seen and url in sources_by_url:
            seen.add(url)
            items.append({"label": label, "url": url})
        else:
            items.append({"label": label})
        if len(items) >= 6:
            break
    if not items:
        return None
    return {
        "type": "prereq",
        "title": _trim_str(card.get("title"), 200)
        or "Before this clicks, you should know",
        "items": items,
    }


def _resolve_gotcha(card: dict) -> dict | None:
    title = _trim_str(card.get("title"), 200)
    wrong = card.get("wrong")
    if not title or not isinstance(wrong, str) or not wrong.strip():
        return None
    return {
        "type": "gotcha",
        "title": title,
        "description": _trim_str(card.get("description"), 400) or "",
        "wrong": wrong,
        "right": _trim_str(card.get("right"), 400) or "",
        "language": _trim_str(card.get("language"), 32) or "",
    }


def _resolve_path_steps(
    links: list[Any], sources_by_url: dict[str, ResourceSource]
) -> list[dict]:
    """Each `links[]` entry may be a URL string or `{url, description, estimate}`."""
    steps: list[dict] = []
    seen: set[str] = set()
    for link in links:
        url = _coerce_path_url(link)
        if not url or url in seen:
            continue
        seen.add(url)
        src = sources_by_url.get(url)
        if src is None:
            continue
        step = _source_to_article_card(src)
        description = _coerce_path_description(link)
        if description:
            step["description"] = description
        estimate_str, estimate_minutes = _coerce_path_estimate(link)
        if estimate_str:
            step["estimate"] = estimate_str
            step["estimate_minutes"] = estimate_minutes
        steps.append(step)
    return steps


def _coerce_path_url(link: Any) -> str | None:
    if isinstance(link, str):
        cleaned = link.strip()
        return cleaned or None
    if isinstance(link, dict):
        url = link.get("url")
        if isinstance(url, str):
            cleaned = url.strip()
            return cleaned or None
    return None


def _coerce_path_description(link: Any) -> str | None:
    if isinstance(link, dict):
        return _trim_str(link.get("description"), 400)
    return None


def _coerce_path_estimate(link: Any) -> tuple[str, int]:
    """Return the step's `(rendered_estimate, minutes)`. ``("", 0)`` on miss."""
    if not isinstance(link, dict):
        return "", 0
    raw = link.get("estimate")
    if not isinstance(raw, str):
        return "", 0
    minutes = _parse_minutes(raw)
    if minutes <= 0:
        return "", 0
    return _format_minutes(minutes), minutes


_ESTIMATE_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b")


def _parse_minutes(text: str) -> int:
    """Parse loose time strings into total minutes.

    Accepts ``"30m"``, ``"45 min"``, ``"1h"``, ``"1.5h"``, ``"1h 30m"``,
    ``"2 hours"``. Multiple tokens sum. Returns 0 if nothing parses.
    """
    if not text:
        return 0
    total = 0.0
    for value, unit in _ESTIMATE_TOKEN_RE.findall(text.lower()):
        try:
            n = float(value)
        except ValueError:
            continue
        if unit.startswith("h"):
            total += n * 60.0
        else:
            total += n
    return int(round(total))


def _format_minutes(minutes: int) -> str:
    """Render minutes back to ``"45m"`` / ``"1h"`` / ``"1h 30m"`` / ``"2h"``."""
    if minutes <= 0:
        return ""
    hours, rem = divmod(minutes, 60)
    if hours == 0:
        return f"{rem}m"
    if rem == 0:
        return f"{hours}h"
    return f"{hours}h {rem}m"


def _trim_str(value: Any, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _validate_category(value: Any) -> str:
    cleaned = _trim_str(value, 64) or ""
    return cleaned if cleaned in _VALID_DIRECTORY_SLUGS else ""
