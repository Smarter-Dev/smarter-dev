"""A model offered its output tool on "auto" may answer in plain text.

Claude Sonnet 5.5 is the only such model today (``supports_forced_tool_choice``
is False because none of its OpenRouter endpoints accepts tool_choice
"required"). pydantic-ai sends a plain-text answer back with a retry prompt,
one output retry each time; these tests pin that the turn survives a slip and that
running out of retries raises — which the chat engine turns into its usual
error reply — rather than ending with no reply.

Unmocked below the HTTP transport: the router, catalog settings, the real
writer agent and pydantic-ai's retry loop all run.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

import smarter_dev.bot.agents.chat_agent as chat_agent
import smarter_dev.bot.agents.writer_agent as writer_agent
from smarter_dev.bot.agents.chat_models import WriterOutput
from smarter_dev.shared import model_router

_SONNET = "anthropic/claude-sonnet-5.5"
_GROK = next(
    m.model_id
    for m in chat_agent.MODEL_CATALOG
    if m.family == "Grok"
)


def _completion(message: dict) -> dict:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1790618686,
        "model": _SONNET,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "message": {"role": "assistant", **message},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


_TEXT = {"content": "Here is my answer in plain text."}
_TOOL = {
    "content": None,
    "tool_calls": [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "final_result",
                "arguments": '{"message": "Here is my answer."}',
            },
        }
    ],
}


@pytest.fixture
def openrouter(monkeypatch):
    """Serve scripted completions to every OpenRouter request; return the log."""
    script: list[dict] = []
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        reply = script.pop(0) if len(script) > 1 else script[0]
        return httpx.Response(200, json=_completion(reply))

    real_provider = model_router.OpenRouterProvider

    def provider_with_transport(**kwargs):
        return real_provider(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kwargs,
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    monkeypatch.setattr(model_router, "OpenRouterProvider", provider_with_transport)
    writer_agent._writer_agents.clear()
    chat_agent._chat_agents.clear()
    chat_agent._worker_agents.clear()
    yield script, sent
    writer_agent._writer_agents.clear()
    chat_agent._chat_agents.clear()
    chat_agent._worker_agents.clear()


async def test_plain_text_answers_are_retried_until_the_output_tool(openrouter):
    script, sent = openrouter
    script.extend([_TEXT, _TEXT, _TOOL])

    result = await writer_agent.get_writer_agent(_SONNET).run("hello")

    assert result.output == WriterOutput(message="Here is my answer.")
    assert len(sent) == 3
    assert all(body["tool_choice"] == "auto" for body in sent)
    # Each slip is sent back with a retry prompt after the text answer.
    assert sent[1]["messages"][-2]["content"] == _TEXT["content"]
    assert sent[1]["messages"][-1]["role"] == "user"
    assert "try again" in sent[1]["messages"][-1]["content"]


async def test_running_out_of_output_retries_raises_instead_of_going_quiet(
    openrouter,
):
    script, sent = openrouter
    script.append(_TEXT)

    with pytest.raises(UnexpectedModelBehavior):
        await writer_agent.get_writer_agent(_SONNET).run("hello")

    # The first answer plus one re-prompt per retry.
    assert len(sent) == 1 + model_router.UNFORCED_OUTPUT_RETRIES


def test_chat_worker_and_writer_agents_widen_output_retries_only_for_sonnet(
    openrouter,
):
    unforced = model_router.UNFORCED_OUTPUT_RETRIES
    assert chat_agent.get_chat_agent(_SONNET)._max_output_retries == unforced
    assert chat_agent.get_worker_agent(_SONNET)._max_output_retries == unforced
    assert writer_agent.get_writer_agent(_SONNET)._max_output_retries == unforced
    # Tool retries keep pydantic-ai's default.
    assert chat_agent.get_chat_agent(_SONNET)._max_tool_retries == 1
    # Models whose output tool is forced keep the default budget.
    assert chat_agent.get_chat_agent(_GROK)._max_output_retries == 1
    assert writer_agent.get_writer_agent(_GROK)._max_output_retries == 1
