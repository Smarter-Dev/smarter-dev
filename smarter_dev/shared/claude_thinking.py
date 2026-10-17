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


def strip_claude_thinking(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The messages without Claude's thinking blocks; ``messages`` if none.

    Text and tool calls stay, and other providers' thinking is untouched. A
    response holding nothing but thinking is kept whole, since an empty
    assistant turn is rejected too.
    """
    stripped: list[ModelMessage] = []
    changed = False
    for message in messages:
        if isinstance(message, ModelResponse):
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
