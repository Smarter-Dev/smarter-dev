"""Public page about the chat agent at /chat-agent (#103).

The prose (the two modes, how memory and dreaming work, opting out) lives in
the template. Below it the page shows the agent's personality, behavior and
memory for the one guild an admin has switched the page on for, with the time
of its last dream. The page 404s while no guild has it on.

Every person in those blocks is masked as "a member" (#104), and every block
shown goes through
:func:`~smarter_dev.web.chat_agent_public.public_text_problem` and the opt-out
gate first; a block that fails is hidden rather than edited. A signed-in
visitor sees their own tags by name, put back on the cached masked blocks per
request; a visitor's render is never cached.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from litestar import Request
from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from skrift.auth.session_keys import SESSION_USER_ID
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.privacy_notice import PRIVACY_PATH
from smarter_dev.web.chat_agent_public import PublicBlock
from smarter_dev.web.chat_agent_public import check_public_blocks
from smarter_dev.web.chat_agent_public import public_guild_memory
from smarter_dev.web.chat_agent_public import viewer_discord_ids

logger = logging.getLogger(__name__)

CHAT_AGENT_PATH = "/chat-agent"
_TITLE = "The chat agent"
_DESCRIPTION = (
    "How Smarter Dev's Discord chat agent works: its two modes, how it "
    "remembers and dreams, and a live look at what it carries forward."
)

# The checked, masked blocks, kept briefly so a crawler cannot turn every hit
# into the engagement-name query and a gate load. Keyed on the blocks' own
# text, so a dream or a purge that changes one is seen on the next hit; the
# switch is read on every hit, so turning the page off is immediate. They are
# the same for every visitor: a visitor's own names go back on per request.
BLOCKS_CACHE_SECONDS = 300
_blocks_cache: dict[tuple, tuple[float, dict[str, PublicBlock]]] = {}


async def _cached_public_blocks(db_session, memory) -> dict[str, PublicBlock]:
    key = (
        memory.guild_id,
        memory.personality or "",
        memory.behavior or "",
        memory.content or "",
    )
    now = time.monotonic()
    hit = _blocks_cache.get(key)
    if hit is not None and now - hit[0] < BLOCKS_CACHE_SECONDS:
        return hit[1]
    blocks = await check_public_blocks(db_session, memory)
    _blocks_cache.clear()
    _blocks_cache[key] = (now, blocks)
    for name, block in blocks.items():
        if block.problem is not None:
            logger.warning(
                "Hiding the %s block on %s: %s", name, CHAT_AGENT_PATH, block.problem
            )
    return blocks


def _session_user_id(request: Request) -> UUID | None:
    raw = request.session.get(SESSION_USER_ID) if request.session else None
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


@get(CHAT_AGENT_PATH)
async def chat_agent_page(request: Request, db_session: AsyncSession) -> Template:
    memory = await public_guild_memory(db_session)
    if memory is None:
        raise NotFoundException()

    blocks = await _cached_public_blocks(db_session, memory)
    viewer_ids = await viewer_discord_ids(db_session, _session_user_id(request))

    url = f"{get_settings().site_base_url.rstrip('/')}{CHAT_AGENT_PATH}"
    return Template(
        "chat_agent.html",
        # A visitor's own names are theirs alone: no shared cache keeps them.
        headers={"Cache-Control": "private, no-store"} if viewer_ids else None,
        context={
            "page_title": _TITLE,
            "personality": blocks["personality"].shown(viewer_ids),
            "behavior": blocks["behavior"].shown(viewer_ids),
            "memory": blocks["memory"].shown(viewer_ids),
            "has_personality": bool(blocks["personality"].masked),
            "has_behavior": bool(blocks["behavior"].masked),
            "has_memory": bool(blocks["memory"].masked),
            "last_dream_at": memory.last_dream_at,
            "privacy_path": PRIVACY_PATH,
            "seo_meta": {
                "description": _DESCRIPTION,
                "canonical_url": url,
                "robots": "index,follow",
            },
            "og_meta": {
                "title": f"{_TITLE} · Smarter Dev",
                "description": _DESCRIPTION,
                "url": url,
                "site_name": "Smarter Dev",
                "type": "website",
                "image": "",
            },
        },
    )
