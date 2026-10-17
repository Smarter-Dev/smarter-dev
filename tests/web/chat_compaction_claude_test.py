"""Web Chat compaction drops the Claude thinking it invalidates (#188)."""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.shared.claude_thinking import strip_claude_thinking
from smarter_dev.web.chat.compaction import KEEP_RECENT_CHARS
from smarter_dev.web.chat.compaction import compact_model_history


def _turn(text: str, *, provider: str = "anthropic") -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(
            parts=[
                ThinkingPart(content="t", signature="sig", provider_name=provider),
                TextPart(content="reply"),
            ],
            provider_name=provider,
        ),
    ]


async def test_kept_turns_lose_claude_thinking_and_keep_their_text():
    big = "x" * (KEEP_RECENT_CHARS // 3)
    history = [message for _ in range(6) for message in _turn(big)]

    async def summarize(transcript: str) -> str:
        return "summary"

    result = await compact_model_history(history, summarize=summarize)

    assert result.changed
    kept = result.messages[1:]
    assert kept
    assert not any(
        isinstance(part, ThinkingPart) for message in kept for part in message.parts
    )
    assert all(
        message.parts == [TextPart(content="reply")]
        for message in kept
        if isinstance(message, ModelResponse)
    )


def test_strip_leaves_other_providers_and_unchanged_histories_alone():
    gemini = _turn("hi", provider="google-gla")
    assert strip_claude_thinking(gemini) is gemini

    only_thinking = [
        ModelResponse(
            parts=[ThinkingPart(content="t", signature="s", provider_name="anthropic")]
        )
    ]
    # An empty assistant turn would be rejected; it stays whole.
    assert strip_claude_thinking(only_thinking) is only_thinking
