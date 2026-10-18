"""A model offered its output tool on "auto" may answer in plain text.

Claude Sonnet and Haiku 5.5 are such models (``supports_forced_tool_choice``
is False because Anthropic answers a forced tool_choice with a 400 on the 5.5
generation). pydantic-ai sends a plain-text answer back with a retry prompt,
one output retry each time; these tests pin that the turn survives a slip and that
running out of retries raises — which the chat engine turns into its usual
error reply — rather than ending with no reply.

Unmocked below the HTTP transport: the router, catalog settings, the real
writer agent and pydantic-ai's retry loop all run.
"""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

import smarter_dev.bot.agents.chat_agent as chat_agent
import smarter_dev.bot.agents.writer_agent as writer_agent
from smarter_dev.bot.agents.chat_models import WriterOutput
from smarter_dev.shared import model_router
from tests.claude_wire import ClaudeServer
from tests.claude_wire import text_reply
from tests.claude_wire import tool_reply

_SONNET = "claude-sonnet-5-5"
_GROK = next(
    m.model_id
    for m in chat_agent.MODEL_CATALOG
    if m.family == "Grok"
)

_TEXT = text_reply("Here is my answer in plain text.")
_TOOL = tool_reply("final_result", {"message": "Here is my answer."})


class _ScriptedServer(ClaudeServer):
    """Replays the last scripted reply once the script runs out."""

    def handle(self, request):
        if len(self.replies) == 1:
            self.replies.append(self.replies[0])
        return super().handle(request)


@pytest.fixture
def anthropic(monkeypatch):
    """Serve scripted replies to every direct-Claude request; return the log."""
    script: list[dict] = []
    server = _ScriptedServer(script)
    server.replies = script

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret")
    monkeypatch.setattr(
        model_router, "AnthropicProvider", lambda *, api_key: server.provider(api_key)
    )
    writer_agent._writer_agents.clear()
    chat_agent._chat_agents.clear()
    chat_agent._worker_agents.clear()
    yield script, server.requests
    writer_agent._writer_agents.clear()
    chat_agent._chat_agents.clear()
    chat_agent._worker_agents.clear()


async def test_plain_text_answers_are_retried_until_the_output_tool(anthropic):
    script, sent = anthropic
    script.extend([_TEXT, _TEXT, _TOOL])

    result = await writer_agent.get_writer_agent(_SONNET).run("hello")

    assert result.output == WriterOutput(message="Here is my answer.")
    assert len(sent) == 3
    assert all(
        body.get("tool_choice", {"type": "auto"})["type"] == "auto" for body in sent
    )
    # Each slip is sent back with a retry prompt after the text answer.
    assert sent[1]["messages"][-2]["role"] == "assistant"
    assert sent[1]["messages"][-2]["content"][0]["text"] == (
        "Here is my answer in plain text."
    )
    assert sent[1]["messages"][-1]["role"] == "user"
    retry = " ".join(
        block.get("text", "") for block in sent[1]["messages"][-1]["content"]
    )
    assert "try again" in retry


async def test_running_out_of_output_retries_raises_instead_of_going_quiet(
    anthropic,
):
    script, sent = anthropic
    script.append(_TEXT)

    with pytest.raises(UnexpectedModelBehavior):
        await writer_agent.get_writer_agent(_SONNET).run("hello")

    # The first answer plus one re-prompt per retry.
    assert len(sent) == 1 + model_router.UNFORCED_OUTPUT_RETRIES


def test_chat_worker_and_writer_agents_widen_output_retries_only_for_sonnet(
    anthropic,
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
