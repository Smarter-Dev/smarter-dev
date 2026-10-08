"""Public page about the chat agent at /chat-agent (#103).

The prose (the two modes, how memory and dreaming work, opting out) lives in
the template. Below it the page shows the agent's personality, behavior and
public memory for the one guild an admin has switched the page on for, with
the time of its last dream. The page 404s while no guild has it on.

The memory block itself is about the people in the guild and is never shown.
Every block shown goes through
:func:`~smarter_dev.web.chat_agent_public.public_text_problem` and the opt-out
gate first, and a block that fails is hidden rather than edited.
"""

from __future__ import annotations

import logging
import time

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.privacy_notice import PRIVACY_PATH
from smarter_dev.web.chat_agent_public import PublicBlock
from smarter_dev.web.chat_agent_public import check_public_blocks
from smarter_dev.web.chat_agent_public import public_guild_memory

logger = logging.getLogger(__name__)

CHAT_AGENT_PATH = "/chat-agent"
_TITLE = "The chat agent"
_DESCRIPTION = (
    "How Smarter Dev's Discord chat agent works: its two modes, how it "
    "remembers and dreams, and a live look at what it carries forward."
)

# The checked blocks, kept briefly so a crawler cannot turn every hit into the
# engagement-name query and a gate load. Keyed on the blocks' own text, so a
# dream or a purge that changes one is seen on the next hit; the switch is
# read on every hit, so turning the page off is immediate.
BLOCKS_CACHE_SECONDS = 300
_blocks_cache: dict[tuple, tuple[float, dict[str, PublicBlock]]] = {}


async def _cached_public_blocks(db_session, memory) -> dict[str, PublicBlock]:
    key = (
        memory.guild_id,
        memory.personality or "",
        memory.behavior or "",
        memory.public_content or "",
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


@get(CHAT_AGENT_PATH)
async def chat_agent_page(db_session: AsyncSession) -> Template:
    memory = await public_guild_memory(db_session)
    if memory is None:
        raise NotFoundException()

    blocks = await _cached_public_blocks(db_session, memory)

    url = f"{get_settings().site_base_url.rstrip('/')}{CHAT_AGENT_PATH}"
    return Template(
        "chat_agent.html",
        context={
            "page_title": _TITLE,
            "personality": blocks["personality"].shown,
            "behavior": blocks["behavior"].shown,
            "public_memory": blocks["public_memory"].shown,
            "has_public_memory": bool(blocks["public_memory"].text),
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
