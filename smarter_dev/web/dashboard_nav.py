"""The dashboard sidebar that Web search and Chat share.

Both live behind one rail of accordion sections, Web search above Chat. The
section for whatever is on screen is open; the other shows only its entry.
``dashboard/index.html`` (search) and ``chat/index.html`` (chat mode) both draw
it from ``dashboard/_nav.html`` with the context built here, so a search page
can list chats and a chat page can list searches without either controller
importing the other's queries.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search.snapshot import summary

RECENT_LIMIT = 20
SEARCH_HOME = "/dashboard/search"
CHAT_HOME = "/dashboard"


async def recent_searches(db_session: AsyncSession, user_id: UUID) -> list[dict]:
    rows = await db_session.scalars(
        select(WebSearchRun)
        .where(WebSearchRun.owner_user_id == user_id)
        .order_by(WebSearchRun.created_at.desc())
        .limit(RECENT_LIMIT)
    )
    return [summary(row) for row in rows]


def _relative(iso: str | None, now: datetime) -> str:
    if not iso:
        return ""
    stamp = datetime.fromisoformat(iso)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    seconds = max(0.0, (now - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{stamp:%b} {stamp.day}"


def recent_meta(row: dict, now: datetime | None = None) -> str:
    """The line under a recent search, as ``dashboard.js`` writes it. Server
    rendered for pages without that script; the search page redraws it."""
    when = _relative(row.get("created_at"), now or datetime.now(UTC))
    if row.get("status") == "answering":
        return f"Writing an answer… · {when}"
    if row.get("active"):
        return f"Searching… · {when}"
    if row.get("status") == "error":
        return f"Failed · {when}"
    relevant = row.get("relevant") or 0
    noun = "relevant result" if relevant == 1 else "relevant results"
    return f"{relevant} {noun} · {when}"


def nav_context(
    *,
    page: str,
    open_section: str,
    recent: list[dict],
    chat: bool,
    chat_actions: bool = False,
    tool: str | None = None,
    search_id: str | None = None,
) -> dict:
    """``page`` is the template drawing it, "dashboard" or "chat", which
    decides whether search links stay in dashboard.js's single-page app.
    ``open_section`` is "search" or "chat". ``chat`` says whether the
    signed-in user may use Chat at all; without it the section is absent.
    ``chat_actions`` adds the rename/archive/delete menus, which only the chat
    page's script can drive."""
    now = datetime.now(UTC)
    return {
        "dashboard_nav": {
            "page": page,
            "open": open_section if chat else "search",
            "chat": chat,
            "chat_actions": chat_actions,
            "tool": tool,
            "search_id": search_id,
            "recent": [{**row, "meta": recent_meta(row, now)} for row in recent],
            "search_home": SEARCH_HOME,
            "chat_home": CHAT_HOME,
        }
    }
