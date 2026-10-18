"""A recorded stand-in for Anthropic's Messages API: no request leaves the box.

The same fake as proactive-agent's ``tests/claude_fakes.py``; ``serve``
points ``model_router``'s Anthropic branch at it.

``ClaudeServer`` answers every ``POST /v1/messages`` with the next queued
reply, records each request body, and speaks both shapes pydantic-ai uses:
a JSON message, or the SSE stream it switches to when ``max_tokens`` is the
model's full output.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import httpx2
from anthropic import AsyncAnthropic
from pydantic_ai.providers.anthropic import AnthropicProvider

FAKE_KEY = "sk-ant-test-not-a-key"


def usage(
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 0,
    cache_write_1h: int = 0,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write_1h,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": cache_write_1h,
        },
    }


def text_reply(text: str, **usage_kwargs) -> dict:
    return {"content": [{"type": "text", "text": text}], "usage": usage(**usage_kwargs)}


def tool_reply(name: str, arguments: dict, **usage_kwargs) -> dict:
    return {
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": name, "input": arguments}
        ],
        "usage": usage(**usage_kwargs),
        "stop_reason": "tool_use",
    }


def _message(model: str, reply: dict) -> dict:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": reply["content"],
        "stop_reason": reply.get("stop_reason", "end_turn"),
        "stop_sequence": None,
        "usage": reply["usage"],
    }


def _sse(model: str, reply: dict) -> bytes:
    message = _message(model, reply)
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {"type": "message_start", "message": {**message, "content": []}},
        )
    ]
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {**block, "input": {}}
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"]),
            }
        events += [
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": message["usage"]["output_tokens"]},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
    ).encode()


class ClaudeServer:
    def __init__(self, replies: Iterable[dict]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages", request.url
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0)
        if body.get("stream"):
            return httpx2.Response(
                200,
                content=_sse(body["model"], reply),
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=_message(body["model"], reply))

    def provider(self, api_key: str | None = None) -> AnthropicProvider:
        """An AnthropicProvider whose client talks only to this fake."""
        client = AsyncAnthropic(
            api_key=api_key or FAKE_KEY,
            base_url="https://claude.invalid",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle)),
        )
        return AnthropicProvider(anthropic_client=client)


def serve(monkeypatch, *replies: dict) -> ClaudeServer:
    """Answer every direct-Claude request ``model_router`` builds from here.

    Records the key each provider was built with in ``server.keys``.
    """
    from smarter_dev.shared import model_router

    server = ClaudeServer(replies)
    server.keys = []

    def provider(*, api_key):
        server.keys.append(api_key)
        return server.provider(api_key)

    monkeypatch.setattr(model_router, "AnthropicProvider", provider)
    return server


def cache_controls(body: dict) -> list[dict]:
    """Every cache_control marker in a request body, in render order."""
    found = [
        tool["cache_control"]
        for tool in body.get("tools", [])
        if "cache_control" in tool
    ]
    system = body.get("system")
    if isinstance(system, list):
        found += [
            block["cache_control"] for block in system if "cache_control" in block
        ]
    for message in body["messages"]:
        content = message["content"]
        if isinstance(content, list):
            found += [
                block["cache_control"] for block in content if "cache_control" in block
            ]
    return found


def thinking_blocks(body: dict) -> list[dict]:
    return [
        block
        for message in body["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") in ("thinking", "redacted_thinking")
    ]
