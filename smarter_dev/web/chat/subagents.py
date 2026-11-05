"""Sub-agent dispatch invariants and structurally composable prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.shared.model_catalog import resolve_reasoning_level

BASE_PROMPT = (
    Path(__file__).parent / "prompts" / "subagents_base_prompt.md"
).read_text(encoding="utf-8")
TOOL_GUIDANCE = """Use tools when they materially improve the answer. Use run_code for safe Python calculations or validation. Keep the user informed through concise tool activity."""
DOCUMENT_GUIDANCE = (
    Path(__file__).parent / "prompts" / "subagents_document_guidance.md"
).read_text(encoding="utf-8")
SUBAGENT_GUIDANCE = """You may dispatch run_subagent(name, task, reasoning_level='inherit'). Put as much relevant context into task as the specialist needs; no conversation context is attached automatically. Names must be unique in this root turn."""
TITLE_GUIDANCE = (
    Path(__file__).parent / "prompts" / "subagents_title_guidance.md"
).read_text(encoding="utf-8")
# The bar for drawing a line is this paragraph, not the code around it. The code
# can only make the tool unavailable where a line is impossible and refuse a
# second call; whether a line is *warranted* is decided here, so it is written
# to describe the judgement rather than the mechanism.
QUICK_THREAD_GUIDANCE = (
    Path(__file__).parent / "prompts" / "subagents_quick_thread_guidance.md"
).read_text(encoding="utf-8")


def effective_system_prompt(
    *, child: bool, needs_title: bool = False, quick_thread: bool = False
) -> str:
    sections = [BASE_PROMPT, TOOL_GUIDANCE]
    if not child:
        sections.extend((DOCUMENT_GUIDANCE, SUBAGENT_GUIDANCE))
        if needs_title:
            sections.append(TITLE_GUIDANCE)
        if quick_thread:
            sections.append(QUICK_THREAD_GUIDANCE)
    return "\n\n".join(sections)


def tool_names_for_child(tool_names: list[str]) -> list[str]:
    """Children cannot create descendants, regardless of prompt behavior."""
    return [name for name in tool_names if name != "run_subagent"]


def child_reasoning(model, parent: ReasoningLevel | None, requested: str = "inherit"):
    if requested == "inherit" or not requested:
        return resolve_reasoning_level(model, parent)
    parsed = parse_reasoning_level(requested)
    if parsed is None:
        raise ValueError("reasoning_level must be 'inherit' or a valid reasoning level")
    return resolve_reasoning_level(model, parsed)


@dataclass(frozen=True, slots=True)
class SubagentDispatch:
    name: str
    task: str
    reasoning_level: str = "inherit"

    def validated(self) -> SubagentDispatch:
        name = self.name.strip()
        task = self.task.strip()
        if not name or len(name) > 100:
            raise ValueError(
                "sub-agent name is required and must be at most 100 characters"
            )
        if not task or len(task) > 20_000:
            raise ValueError(
                "sub-agent task is required and must be at most 20,000 characters"
            )
        if any(ord(char) < 32 for char in name):
            raise ValueError("sub-agent name contains control characters")
        return SubagentDispatch(name, task, self.reasoning_level)


async def run_subagent(name: str, task: str, reasoning_level: str = "inherit") -> str:
    """Canonical tool signature; runtime injects durable dispatch dependencies."""
    SubagentDispatch(name, task, reasoning_level).validated()
    return "Sub-agent dispatch is unavailable outside an active Chat root turn."
