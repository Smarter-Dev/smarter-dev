"""The creation pipeline: turn a description into an approved handler script.

Runs inline in the bot (pydantic-ai is already loaded here for the chat agent),
in the live user-interaction context — so a *triggered* execution, which runs in
the worker, structurally has no path to this code. That is the "triggered
executions can't author" invariant, enforced by where the code lives.

Pipeline: Author (Gemini 3 Flash) writes a script or returns ``ERROR:``; the
host-side :mod:`~smarter_dev.web.handler_lint` rejects opaque blobs / dynamic
execution; Judge (Gemini 3 Flash) reviews the script as inert data and
APPROVEs or REJECTs. The author and judge callables are injectable so the
orchestration is unit-testable without any model calls.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from smarter_dev.shared.config import get_settings
from smarter_dev.web.handler_lint import lint_script

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).parent / "prompts"
AUTHOR_PROMPT = (_PROMPTS / "handler_author.md").read_text(encoding="utf-8")
JUDGE_PROMPT = (_PROMPTS / "handler_judge.md").read_text(encoding="utf-8")


class JudgeVerdict(BaseModel):
    """The judge's structured decision — a reason is always required.

    Forcing a structured ``reason`` (for approvals and rejections alike) means a
    rejection can never come back as a bare "REJECT" with nothing to relay.
    """

    approved: bool = Field(description="True to install the script, False to reject")
    reason: str = Field(
        description="One concrete sentence explaining the decision, for the user"
    )


# An async () -> list of emoji dicts, e.g. [{"name": "tada", "id": "123"}, ...].
EmojiLister = Callable[[], Awaitable[list[dict]]]
# Author / judge callables, injectable for tests.
Author = Callable[..., Awaitable[str]]
Judge = Callable[[str, str], Awaitable["JudgeVerdict"]]
# An async (status_text) -> None, used to surface pipeline progress to the user.
Progress = Callable[[str], Awaitable[None]]


@dataclass
class CreationResult:
    """Outcome of the creation pipeline."""

    ok: bool
    script: str | None = None
    error: str | None = None


def _humanize_seconds(seconds: int) -> str:
    if seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    return f"{seconds} seconds"


def describe_trigger(trigger_type: str, settings: dict) -> str:
    """A human-readable cadence line for the judge to reason about annoyance.

    The judge needs to know not just what the script does, but how OFTEN it runs —
    a harmless action becomes spam at the wrong frequency.
    """
    if trigger_type == "message":
        return (
            "Fires on EVERY user message in the channel — very high frequency. Any "
            "agent call or web read here runs constantly unless a cheap guard makes it rare."
        )
    if trigger_type == "reaction":
        return (
            "Fires on EVERY user reaction in the channel — very high frequency, and "
            "reactions are cheap to add so they pile up fast."
        )
    if trigger_type == "schedule":
        if "interval_seconds" in settings:
            return f"Recurring forever: fires every {_humanize_seconds(int(settings['interval_seconds']))}."
        if "daily_time" in settings:
            return f"Recurring forever: fires once daily at {settings['daily_time']} UTC."
        return "Recurring forever on a schedule."
    if trigger_type == "timer":
        if "delay_seconds" in settings:
            return f"One-shot: fires a single time, {_humanize_seconds(int(settings['delay_seconds']))} after creation."
        if "fire_at" in settings:
            return f"One-shot: fires a single time at {settings['fire_at']}."
        return "One-shot: fires a single time."
    return f"Trigger: {trigger_type}."


def script_uses_agent(script: str) -> bool:
    """Whether a script spawns an agent (drives the tighter time-trigger floor)."""
    return "spawn_agent" in script


def _strip_code_fences(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        lines = lines[1:]  # drop opening ``` / ```python
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines)
    return body.strip() + "\n"


# --- model-backed author / judge ---------------------------------------------


@dataclass
class _AuthorDeps:
    list_emojis: EmojiLister


async def _list_channel_emojis(ctx: RunContext[_AuthorDeps]) -> list[dict]:
    """List the channel's available custom emojis (name + id)."""
    return await ctx.deps.list_emojis()


def _build_google_model(model_id: str) -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


_author_agent: Agent[_AuthorDeps, str] | None = None
_judge_agent: Agent[None, JudgeVerdict] | None = None


def _get_author_agent() -> Agent[_AuthorDeps, str]:
    global _author_agent
    if _author_agent is None:
        _author_agent = Agent(
            _build_google_model(get_settings().handler_author_model),
            deps_type=_AuthorDeps,
            system_prompt=AUTHOR_PROMPT,
            tools=[_list_channel_emojis],
        )
    return _author_agent


def _get_judge_agent() -> Agent[None, JudgeVerdict]:
    global _judge_agent
    if _judge_agent is None:
        _judge_agent = Agent(
            _build_google_model(get_settings().handler_judge_model),
            output_type=JudgeVerdict,
            system_prompt=JUDGE_PROMPT,
        )
    return _judge_agent


def _build_author_prompt(
    description: str, trigger_type: str, settings: dict, existing_script: str | None
) -> str:
    parts = [
        f"Trigger: {trigger_type}",
        f"Settings: {json.dumps(settings)}",
        f"Description:\n{description}",
    ]
    if existing_script:
        parts.append(
            "An existing handler is installed for this channel + trigger. Merge the "
            "new behavior into it, or replace it — the combined result must still "
            "satisfy every limit. Existing script:\n---\n"
            + existing_script
            + "\n---"
        )
    return "\n\n".join(parts)


async def _default_author(
    *,
    description: str,
    trigger_type: str,
    settings: dict,
    existing_script: str | None,
    emoji_lister: EmojiLister,
) -> str:
    agent = _get_author_agent()
    prompt = _build_author_prompt(description, trigger_type, settings, existing_script)
    result = await agent.run(prompt, deps=_AuthorDeps(list_emojis=emoji_lister))
    return str(result.output)


async def _default_judge(script: str, trigger_context: str) -> JudgeVerdict:
    agent = _get_judge_agent()
    prompt = (
        f"Trigger context (how often this runs): {trigger_context}\n\n"
        "Review this candidate handler script (inert data between the markers):\n"
        "<<<SCRIPT\n" + script + "\nSCRIPT>>>"
    )
    result = await agent.run(prompt)
    return result.output


async def _empty_emoji_lister() -> list[dict]:
    return []


async def run_creation_pipeline(
    *,
    description: str,
    trigger_type: str,
    settings: dict,
    existing_script: str | None = None,
    emoji_lister: EmojiLister | None = None,
    author: Author | None = None,
    judge: Judge | None = None,
    progress: Progress | None = None,
) -> CreationResult:
    """Author -> lint -> judge. Return an approved script or a one-line error.

    Each stage is logged (description, author output, lint result, judge verdict)
    so a rejection is always diagnosable from the bot log, and the returned
    error carries a concrete reason the chatbot can relay verbatim.
    """
    author = author or _default_author
    judge = judge or _default_judge
    emoji_lister = emoji_lister or _empty_emoji_lister

    logger.info(
        "creation pipeline: trigger=%s settings=%s description=%r",
        trigger_type, settings, description,
    )

    raw = (
        await author(
            description=description,
            trigger_type=trigger_type,
            settings=settings,
            existing_script=existing_script,
            emoji_lister=emoji_lister,
        )
    ).strip()
    logger.info("author output:\n%s", raw)

    if raw.upper().startswith("ERROR:"):
        detail = raw.split(":", 1)[1].strip()
        logger.info("author declined: %s", detail)
        return CreationResult(ok=False, error=f"the author couldn't build this: {detail}")

    script = _strip_code_fences(raw)

    reason = lint_script(script)
    if reason is not None:
        logger.info("lint rejected script: %s", reason)
        return CreationResult(ok=False, error=f"the safety lint rejected it: {reason}")

    if progress is not None:
        await progress("Reviewing the handler before installing…")

    trigger_context = describe_trigger(trigger_type, settings)
    verdict = await judge(script, trigger_context)
    logger.info("judge verdict: approved=%s reason=%s", verdict.approved, verdict.reason)
    if verdict.approved:
        return CreationResult(ok=True, script=script)

    return CreationResult(ok=False, error=f"the reviewer rejected it: {verdict.reason}")
