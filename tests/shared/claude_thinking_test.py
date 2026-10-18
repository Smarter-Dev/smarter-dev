"""Which Claude thinking blocks a request may carry (#199 review).

A block is signed against its model and the exact prefix before it, the block
before it included. An edit takes every block after it; each model is sent
only its own blocks; a response left empty goes without breaking tool
pairing; a summarizer is sent no thinking at all.
"""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import RetryPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.shared.claude_thinking import drop_other_models_claude_thinking
from smarter_dev.shared.claude_thinking import strip_claude_thinking
from smarter_dev.shared.claude_thinking import without_thinking

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"


def _ask(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(text)])


def _thought(text: str, provider: str = "anthropic") -> ThinkingPart:
    return ThinkingPart(content=text, signature=f"sig-{text}", provider_name=provider)


def _reply(model: str, *parts) -> ModelResponse:
    return ModelResponse(parts=list(parts), model_name=model, provider_name="anthropic")


def _thoughts(messages) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ThinkingPart)
    ]


def test_a_round_trip_sends_each_model_only_its_own_blocks():
    history = [
        _ask("a"),
        _reply(OPUS, _thought("opus-1"), TextPart("one")),
        _ask("b"),
        _reply(SONNET, _thought("sonnet-1"), TextPart("two")),
        _ask("c"),
        _reply(OPUS, _thought("opus-2"), TextPart("three")),
    ]

    assert _thoughts(drop_other_models_claude_thinking(history, OPUS)) == [
        "opus-1",
        "opus-2",
    ]
    assert _thoughts(drop_other_models_claude_thinking(history, SONNET)) == ["sonnet-1"]
    # The stored history is never edited.
    assert _thoughts(history) == ["opus-1", "sonnet-1", "opus-2"]
    # Another provider's thinking is not Claude's to judge.
    other = [_ask("a"), _reply("gpt-6-luna", _thought("luna", "openai"), TextPart("x"))]
    assert drop_other_models_claude_thinking(other, OPUS) is other


def test_a_thinking_only_response_goes_and_its_neighbours_merge():
    """Pydantic-ai asks again after a reply with no text; dropping that reply
    leaves two requests in a row, which become one."""
    retry = ModelRequest(parts=[RetryPromptPart("Please answer.")])
    history = [
        _ask("a"),
        _reply(SONNET, _thought("only")),
        retry,
        _reply(SONNET, TextPart("answer")),
        _ask("b"),
    ]

    sent = drop_other_models_claude_thinking(history, OPUS)

    assert [type(message).__name__ for message in sent] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
    ]
    assert [part.part_kind for part in sent[0].parts] == ["user-prompt", "retry-prompt"]
    assert sent[1].parts == [TextPart("answer")]


def test_tool_results_stay_first_after_the_answering_call():
    history = [
        _ask("a"),
        _reply(OPUS, ToolCallPart("search", {"q": "x"}, tool_call_id="t1")),
        ModelRequest(parts=[ToolReturnPart("search", "found", tool_call_id="t1")]),
        _reply(OPUS, _thought("mulling")),
        _ask("b"),
    ]

    stripped = strip_claude_thinking(history, start=2)

    call, results = stripped[1], stripped[2]
    assert isinstance(call.parts[0], ToolCallPart)
    assert [part.part_kind for part in results.parts] == ["tool-return", "user-prompt"]
    assert len(stripped) == 3


def test_an_edit_takes_every_block_after_it_and_none_before():
    history = [
        _ask("a"),
        _reply(OPUS, _thought("before"), TextPart("one")),
        _ask("b"),
        _reply(OPUS, _thought("after-1"), TextPart("two")),
        _ask("c"),
        _reply(OPUS, _thought("after-2"), TextPart("three")),
    ]

    assert _thoughts(strip_claude_thinking(history, start=2)) == ["before"]
    head = history[:2]
    assert strip_claude_thinking(head, start=2) is head


def test_a_summarizer_gets_no_thinking_from_anyone():
    history = [
        _ask("a"),
        _reply(OPUS, _thought("signed")),
        _ask("b"),
        _reply("gpt-6-luna", _thought("luna", "openai"), TextPart("text")),
    ]

    sent = without_thinking(history)

    assert _thoughts(sent) == []
    assert [type(message).__name__ for message in sent] == [
        "ModelRequest",
        "ModelResponse",
    ]
    assert sent[1].parts == [TextPart("text")]
