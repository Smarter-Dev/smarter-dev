"""Claude thinking blocks only replay where they are still valid.

A Claude 5.5 thinking block is signed against the model and the exact
conversation prefix that produced it: the system prompt, the tools, every
earlier message and the thinking block before it. Replayed after that prefix
changes, it is a 400 on accounts Anthropic enforces, which includes the account
our Claude key belongs to. Two rules follow.

- An edit invalidates every block after it, so blocks go from the edit to the
  end, never from the middle alone (``strip_claude_thinking`` with ``start``).
  Keep-tail compaction is such an edit for every turn it keeps.
- Each model is sent only its own blocks (``drop_other_models_claude_thinking``),
  computed from the stored history on every request, so a model's blocks
  always see the prefix they were signed with: A, then B, then A again sends A
  its own blocks and never B's.

A response left with nothing once its thinking goes is dropped, and the
requests either side of it become one, so no empty assistant turn is sent and
tool calls stay next to their results (a thinking-only response holds no tool
call). Anything this misses, pydantic-ai retries once with
``prefix_mismatch_behavior="drop_block"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ModelResponsePart
from pydantic_ai.messages import ThinkingPart


def _is_claude_thinking(part: ModelResponsePart) -> bool:
    return isinstance(part, ThinkingPart) and part.provider_name == "anthropic"


def _without_parts(
    messages: list[ModelMessage],
    drops: Callable[[int, ModelResponse, ModelResponsePart], bool],
) -> list[ModelMessage]:
    """``messages`` minus the response parts ``drops`` names; ``messages``
    itself when it names none. A response left empty is dropped and the
    requests either side of it merged."""
    kept: list[ModelMessage] = []
    changed = False
    merge_next = False
    for index, message in enumerate(messages):
        if isinstance(message, ModelResponse):
            parts = [part for part in message.parts if not drops(index, message, part)]
            if len(parts) != len(message.parts):
                changed = True
                if not parts:
                    merge_next = bool(kept) and isinstance(kept[-1], ModelRequest)
                    continue
                message = replace(message, parts=parts)
        elif merge_next and isinstance(message, ModelRequest):
            # The later request keeps its instructions and metadata; the
            # earlier one's parts (tool results first) lead.
            message = replace(message, parts=[*kept.pop().parts, *message.parts])
        merge_next = False
        kept.append(message)
    return kept if changed else messages


def strip_claude_thinking(
    messages: list[ModelMessage], *, start: int = 0
) -> list[ModelMessage]:
    """The messages without Claude's thinking blocks from ``start`` on;
    ``messages`` itself if there were none.

    Text and tool calls stay, and other providers' thinking is untouched.
    """
    return _without_parts(
        messages, lambda index, _, part: index >= start and _is_claude_thinking(part)
    )


def without_thinking(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The messages with no thinking at all, from any provider.

    For a summarizer: it reads the transcript as a new conversation, where no
    block's prefix holds, and the reasoning is not what it summarizes.
    """
    return _without_parts(messages, lambda _, __, part: isinstance(part, ThinkingPart))


def drop_other_models_claude_thinking(
    history: list[ModelMessage], model_name: str | None
) -> list[ModelMessage]:
    """The history as ``model_name`` is sent it: its own Claude thinking only.

    Every Claude block another model wrote goes, wherever it sits, and the
    model's own blocks stay. Computed the same way on every request, each
    model's blocks keep the prefix they were signed with: the other models'
    blocks were already absent when they were written. ``history`` itself is
    returned when there is nothing to drop.
    """
    if not isinstance(model_name, str) or not model_name:
        return history
    return _without_parts(
        history,
        lambda _, message, part: _is_claude_thinking(part)
        and not (message.model_name or "").startswith(model_name),
    )
