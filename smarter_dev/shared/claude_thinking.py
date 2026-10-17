"""Claude thinking blocks only replay where they are still valid.

A Claude 5.5 thinking block is signed against the model and the exact
conversation prefix that produced it: the system prompt, the tools and every
earlier message. Replayed after that prefix changes, it is a 400 on accounts
Anthropic enforces, which includes the account our Claude key belongs to.
Keep-tail compaction changes the prefix of every turn it keeps, so it strips
their blocks here. Anything this misses, pydantic-ai retries once with
``prefix_mismatch_behavior="drop_block"``.
"""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ThinkingPart


def strip_claude_thinking(
    messages: list[ModelMessage], *, start: int = 0
) -> list[ModelMessage]:
    """The messages without Claude's thinking blocks from ``start`` on;
    ``messages`` itself if there were none.

    Text and tool calls stay, and other providers' thinking is untouched. A
    response holding nothing but thinking is kept whole, since an empty
    assistant turn is rejected too.
    """
    stripped: list[ModelMessage] = []
    changed = False
    for index, message in enumerate(messages):
        if index >= start and isinstance(message, ModelResponse):
            parts = [
                part
                for part in message.parts
                if not (
                    isinstance(part, ThinkingPart) and part.provider_name == "anthropic"
                )
            ]
            if parts and len(parts) != len(message.parts):
                message = replace(message, parts=parts)
                changed = True
        stripped.append(message)
    return stripped if changed else messages


def drop_other_models_claude_thinking(
    history: list[ModelMessage], model_name: str | None
) -> list[ModelMessage]:
    """Claude thinking replays only on the model that wrote it.

    After a history moves to a Claude model (PROACTIVE_AGENT_MODEL), the
    blocks it opened with, from the earlier model, are dropped rather than
    sent to a model they are not bound to. Only that leading run goes:
    removing blocks from the front leaves the later ones valid, removing them
    from the middle would not. A foreign stretch further in (after switching
    back) is left to the API, which ignores blocks from another model.
    """
    if not model_name:
        return history
    for index, message in enumerate(history):
        if isinstance(message, ModelResponse) and (message.model_name or "").startswith(
            model_name
        ):
            head = history[:index]
            stripped = strip_claude_thinking(head)
            return history if stripped is head else [*stripped, *history[index:]]
    return strip_claude_thinking(history)
