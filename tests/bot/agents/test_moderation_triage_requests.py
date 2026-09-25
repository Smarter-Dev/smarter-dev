"""Moderation triage against a local stand-in for OpenAI's chat completions API.

The real path runs end to end: ``get_llm_model`` builds the ``dspy.LM``, DSPy
ReAct drives it, litellm sends the HTTP request. Only the server is fake. Like
OpenAI, it refuses ``max_tokens`` from a reasoning model with a 400, which is
how triage failed on GPT-6 Luna on 2026-09-25.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

from smarter_dev.bot.agents import moderation_agent
from smarter_dev.bot.agents.mod_tools import ActionTracker
from smarter_dev.bot.agents.mod_tools import build_triage_report_embed
from smarter_dev.llm_config import get_llm_model

_MAX_TOKENS_ERROR = {
    "error": {
        "message": "Unsupported parameter: 'max_tokens' is not supported with this model."
        " Use 'max_completion_tokens' instead.",
        "type": "invalid_request_error",
        "param": "max_tokens",
        "code": "unsupported_parameter",
    }
}

# ChatAdapter sections for a ReAct step, then the extract step.
_FINISH = (
    "[[ ## next_thought ## ]]\nNothing needs doing.\n\n"
    "[[ ## next_tool_name ## ]]\nfinish\n\n"
    "[[ ## next_tool_args ## ]]\n{}\n\n"
    "[[ ## completed ## ]]"
)
_EXTRACT = (
    "[[ ## reasoning ## ]]\nA friendly exchange.\n\n"
    "[[ ## assessment ## ]]\nFriendly banter, nothing to act on.\n\n"
    "[[ ## completed ## ]]"
)


class _FakeOpenAI:
    """Replays ``replies`` in order; a ``None`` reply is a 500."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.bodies.append(body)
                if "max_tokens" in body and body["model"].startswith(("gpt-5", "gpt-6", "o")):
                    self._send(400, _MAX_TOKENS_ERROR)
                    return
                content = fake.replies.pop(0) if fake.replies else None
                if content is None:
                    self._send(500, {"error": {"message": "boom", "type": "server_error"}})
                    return
                self._send(200, {
                    "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                    "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def openai_stub(monkeypatch):
    servers = []

    def start(model: str, replies):
        server = _FakeOpenAI(replies)
        servers.append(server)
        monkeypatch.setenv("LLM_MEDIUM_MODEL", model)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_BASE", server.url)
        lm = get_llm_model("medium")
        lm.cache = False  # every call must reach the stub
        lm.num_retries = 0
        monkeypatch.setattr(moderation_agent, "MODERATION_LM", lm)
        return server

    yield start
    for server in servers:
        server.close()


async def _triage():
    return await moderation_agent.run_moderation_agent(
        bot=MagicMock(),
        guild_id="1",
        channel_id="2",
        trigger_message_content="@mods can someone look at this?",
        trigger_author="reporter",
        context_messages=[],
        guild_instructions="",
        enabled_tools=[],
        trigger_message_id="3",
    )


def _field_names(embed) -> list[str]:
    return [f.name for f in embed.fields]


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna"])
async def test_triage_sends_max_completion_tokens_to_openai_reasoning_models(openai_stub, model):
    server = openai_stub(model, [_FINISH, _EXTRACT])

    assessment, tracker = await _triage()

    assert tracker.failure is None
    assert assessment == "Friendly banter, nothing to act on."
    assert len(server.bodies) == 2
    for body in server.bodies:
        assert body["model"] == model
        assert body["max_completion_tokens"] == 25000
        assert "max_tokens" not in body


def test_other_providers_keep_max_tokens(monkeypatch):
    # OpenRouter accepts max_tokens for reasoning models; non-reasoning models
    # keep DSPy's default limit. Neither is rewritten.
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    for model, limit in (("openrouter/openai/gpt-6-luna", 25000), ("gemini/gemini-3.8-flash", 4000)):
        monkeypatch.setenv("LLM_MEDIUM_MODEL", model)
        lm = get_llm_model("medium")
        assert lm.kwargs["max_tokens"] == limit
        assert "max_completion_tokens" not in lm.kwargs


async def test_a_failed_triage_reports_manual_review_not_no_action(openai_stub):
    openai_stub("gpt-6-luna", [None])

    assessment, tracker = await _triage()

    assert tracker.failure == "InternalServerError"
    assert not tracker.has_actions
    assert "boom" not in assessment
    embed = build_triage_report_embed(tracker, assessment, "reporter", "2", "3")
    assert embed.title == "Moderation Triage Failed"
    assert "No Action Taken" not in _field_names(embed)
    [status] = [f for f in embed.fields if f.name == "Manual Review Required"]
    assert "not reviewed" in status.value
    assert "no immediate action required" not in json.dumps([f.value for f in embed.fields])


@pytest.mark.parametrize("notice", [None, "A moderator will be here shortly."])
def test_a_triage_failing_after_an_action_keeps_the_action_in_the_report(notice):
    # The tracker a failed run returns still holds what executed before the
    # failure; the report lists it and says whether the channel was told.
    tracker = ActionTracker(flags=["42"], channel_message=notice, failure="BadRequestError")
    embed = build_triage_report_embed(
        tracker, "Moderation triage failed (BadRequestError).", "reporter", "2", "3"
    )
    names = _field_names(embed)
    assert embed.title == "Moderation Triage Failed"
    assert "Flagged for Review" in names
    assert "No Action Taken" not in names
    [status] = [f for f in embed.fields if f.name == "Manual Review Required"]
    assert "already taken" in status.value
    assert ("No notice was posted" in status.value) is (notice is None)


async def test_a_failed_triage_returns_the_tracker_it_ran_with(openai_stub, monkeypatch):
    from smarter_dev.bot.agents import mod_tools

    created = []
    real = mod_tools.create_moderation_tools

    def spy(**kwargs):
        tools, tracker = real(**kwargs)
        tracker.flags.append("42")  # stands in for an action that ran
        created.append(tracker)
        return tools, tracker

    monkeypatch.setattr(moderation_agent, "create_moderation_tools", spy)
    openai_stub("gpt-6-luna", [None])

    _, tracker = await _triage()

    assert tracker is created[0]
    assert tracker.flags == ["42"]
    assert tracker.failure == "InternalServerError"


def test_a_finished_triage_with_no_action_still_says_so():
    embed = build_triage_report_embed(ActionTracker(), "All fine.", "reporter", "2", "3")
    assert embed.title == "Moderation Triage Report"
    assert "No Action Taken" in _field_names(embed)
    assert "Manual Review Required" not in _field_names(embed)
