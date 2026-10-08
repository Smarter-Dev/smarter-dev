#!/usr/bin/env python3
"""One-off: rewrite stored chat-agent memory into the ``<id:username>`` tag form (#104).

Converts ``username (id N)``, and every ``<@N>`` mention whose name is known,
to ``<N:username>`` in every guild's memory, behavior and personality blocks
and pending notes (see :mod:`smarter_dev.web.chat_memory_retag`). A dry run by
default: it logs, per guild, how many references each column would convert,
how many known usernames would still appear outside a tag, and whether a
column would go over its cap, then rolls back. ``--apply`` writes each guild
in its own transaction with a memory revision, and refuses (leaves untouched)
a guild with a column that would go over its cap. Counts only, never text.
Running it again converts nothing. Exits 1 when ``--apply`` refused a guild.

Run it after the deploy that carries #104, as the Job in
``k8s/oneoff-retag-chat-agent-memory.yaml``. Locally:
    .venv/bin/python scripts/retag_chat_agent_memory.py
    .venv/bin/python scripts/retag_chat_agent_memory.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.chat_memory_retag import GuildRetag
from smarter_dev.web.chat_memory_retag import apply_guild
from smarter_dev.web.chat_memory_retag import guild_ids
from smarter_dev.web.chat_memory_retag import plan_guild

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("retag_chat_agent_memory")


def _counts(values: dict[str, int]) -> str:
    return " ".join(f"{column}={count}" for column, count in values.items())


def report(plan: GuildRetag, *, applied: bool) -> None:
    if plan.refused:
        verdict = "refused, over cap in " + ",".join(plan.over_cap)
    elif plan.total == 0:
        verdict = "nothing to convert"
    else:
        verdict = "rewritten" if applied else "would rewrite"
    logger.info(
        "guild %s: %s; converted %s; untagged known names %s",
        plan.guild_id,
        verdict,
        _counts(plan.converted),
        _counts(plan.untagged),
    )


async def run(session_factory, *, apply: bool) -> list[GuildRetag]:
    """Plan (or apply) every guild, one session each."""
    async with session_factory() as session:
        guilds = await guild_ids(session)
    plans = []
    for guild_id in guilds:
        async with session_factory() as session:
            if apply:
                plan = await apply_guild(session, guild_id)
            else:
                plan = await plan_guild(session, guild_id)
                await session.rollback()
        report(plan, applied=apply)
        plans.append(plan)
    return plans


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would change and write nothing (the default)",
    )
    mode.add_argument("--apply", action="store_true", help="write the rewrite")
    args = parser.parse_args(argv)
    plans = await run(get_db_session_context, apply=args.apply)
    refused = [plan for plan in plans if plan.refused]
    logger.info(
        "%s %d guild(s): %d converted, %d refused",
        "applied to" if args.apply else "dry run over",
        len(plans),
        sum(1 for plan in plans if plan.total and not plan.refused),
        len(refused),
    )
    return 1 if args.apply and refused else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("retag failed")
        sys.exit(1)
